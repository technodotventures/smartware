"""Import a Letta agent file (``.af``) into ORP (adapter level L2).

* Core memory blocks (``persona``, ``human`` and any custom label) become ``kind: "profile"`` records with
  ``load: "always"``, so ``profile()`` returns them. The block label, limit and description go in ``meta``.
* Messages become ``event`` records in a thread named after the agent, in their original order, with the original
  times as ``event_time``. Letta's JSON-wrapped user messages are unwrapped; an assistant's ``send_message`` tool
  call becomes the assistant's reply. System prompts, tool results and heartbeats are skipped by default.
* Archival passages, where the file carries them, become ``document`` records.

The file is read as data: nothing in it is evaluated, and tools (which carry source code) are never imported.
Every record is written with ``origin: "imported"``, readable only by you unless you pass ``readers`` or
``audience="tenant"``, and with idempotency keys derived from the content, so importing the same file twice writes
nothing new.

Targeted format: the single-agent ``AgentSchema`` that Letta 0.6 and 0.7 export (top-level ``core_memory``,
``messages``, ``name``, ``version``), plus the later multi-entity layout (top-level ``agents`` and ``blocks`` with
``block_ids``). Archival passages are read from ``passages`` or ``archival_memory`` lists if present; check this
against a current Letta export before relying on it.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple, Union

from .client import OrpClient, _UNSET

LEVEL = "L2"
MAX_FILE_BYTES = 256 << 20
_SKIP_USER_TYPES = {"heartbeat", "login", "system_alert"}


def _enc(s: str) -> str:
    return urllib.parse.quote(str(s), safe="").replace("~", "%7E")


def _text(content: Any) -> str:
    """Letta message content: a string, or a list of parts such as ``{"type": "text", "text": ...}``."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type", "text") == "text"
                         and isinstance(p.get("text"), str))
    return ""


def _load(src: Union[str, "os.PathLike[str]", Dict[str, Any]]) -> Dict[str, Any]:
    if isinstance(src, dict):
        return src
    path = os.fspath(src)
    if os.path.getsize(path) > MAX_FILE_BYTES:
        raise ValueError(f"agent file is over {MAX_FILE_BYTES} bytes")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("an agent file is a JSON object")
    return data


def _agents(data: Dict[str, Any]) -> Tuple[str, List[Tuple[Dict[str, Any], List[Dict[str, Any]]]]]:
    """(format, [(agent, blocks)]) for either layout."""
    if isinstance(data.get("agents"), list):
        blocks = {b.get("id"): b for b in data.get("blocks") or [] if isinstance(b, dict)}
        out = []
        for a in data["agents"]:
            if not isinstance(a, dict):
                continue
            bl = [blocks[i] for i in a.get("block_ids") or [] if i in blocks]
            bl += [b for b in a.get("core_memory") or [] if isinstance(b, dict)]
            out.append((a, bl))
        return "multi", out
    if "core_memory" in data or "messages" in data:
        return "agent", [(data, [b for b in data.get("core_memory") or [] if isinstance(b, dict)])]
    raise ValueError("not a Letta agent file: expected core_memory/messages or agents/blocks")


def _message(m: Dict[str, Any], include_reasoning: bool) -> Tuple[Optional[str], str, str]:
    """(text or None to skip, role, skip reason)."""
    role = m.get("role")
    text = _text(m.get("content", m.get("text")))
    if role == "system":
        return None, "system", "system_messages"
    if role == "tool":
        return None, "tool", "tool_messages"
    if role == "user":
        try:
            wrapped = json.loads(text)
        except (ValueError, TypeError):
            wrapped = None
        if isinstance(wrapped, dict) and "type" in wrapped:
            if wrapped.get("type") in _SKIP_USER_TYPES:
                return None, "user", "heartbeats"
            if isinstance(wrapped.get("message"), str):
                text = wrapped["message"]
        return (text if text.strip() else None), "user", "empty_messages"
    if role == "assistant":
        reply = None
        for tc in m.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            if fn.get("name") == "send_message":
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                if isinstance(args.get("message"), str):
                    reply = args["message"]
        if reply is None and not (m.get("tool_calls") or []):
            reply = text  # a plain assistant message (newer Letta agents reply without send_message)
        parts = []
        if include_reasoning and text.strip() and reply != text:
            parts.append(f"(thinking) {text}")
        if reply:
            parts.append(reply)
        body = "\n".join(parts)
        return (body if body.strip() else None), "assistant", "tool_calls_without_reply"
    return None, str(role), "other_roles"


def _key(agent_key: str, part: str, payload: Any) -> str:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]
    return f"letta:{agent_key}:{part}:{digest}"


def _write(client: OrpClient, mems: List[Dict[str, Any]], agent_key: str, part: str, batch: int,
           kw: Dict[str, Any]) -> Tuple[List[str], int]:
    ids: List[str] = []
    dedup = 0
    for i in range(0, len(mems), batch):
        chunk = mems[i:i + batch]
        r = client.remember(chunk, idempotency_key=_key(agent_key, f"{part}:{i // batch}", chunk), mode="append",
                            **kw)
        ids += r.ids
        dedup += len(r.deduplicated)
    return ids, dedup


def import_af(path_or_dict: Union[str, "os.PathLike[str]", Dict[str, Any]], client: OrpClient, *,
              readers: Any = _UNSET, audience: Optional[str] = None, include_reasoning: bool = False,
              batch_size: int = 100) -> Dict[str, Any]:
    """Import a Letta agent file. Returns what was written, per agent, and what was skipped and why."""
    data = _load(path_or_dict)
    fmt, agents = _agents(data)
    kw: Dict[str, Any] = {}
    if readers is not _UNSET:
        kw["readers"] = readers
    if audience is not None:
        kw["audience"] = audience
    summaries = []
    for agent, blocks in agents:
        name = str(agent.get("name") or agent.get("id") or "letta-agent")
        agent_key = _enc(str(agent.get("id") or name))
        scope = f"letta/{_enc(name)}"
        skipped: Dict[str, int] = {}

        def skip(reason: str) -> None:
            skipped[reason] = skipped.get(reason, 0) + 1

        profiles: List[Dict[str, Any]] = []
        for b in blocks:
            value, label = b.get("value"), b.get("label") or "block"
            if not isinstance(value, str) or not value.strip():
                skip("empty_blocks")
                continue
            meta = {"block_label": label, "letta_agent": name}
            if b.get("limit") is not None:
                meta["block_limit"] = b.get("limit")
            if b.get("description"):
                meta["block_description"] = b.get("description")
            profiles.append({"kind": "profile", "load": "always", "origin": "imported", "content": value,
                             "scope": scope, "thread": f"{name}/core-memory",
                             "sources": [{"uri": f"letta-af://{_enc(name)}/blocks/{_enc(label)}"}], "meta": meta})

        events: List[Dict[str, Any]] = []
        for n, m in enumerate(agent.get("messages") or []):
            if not isinstance(m, dict):
                continue
            text, role, reason = _message(m, include_reasoning)
            if text is None:
                skip(reason)
                continue
            ev: Dict[str, Any] = {"kind": "event", "origin": "imported", "content": text, "role": role,
                                  "actor": name if role == "assistant" else (m.get("name") or "user"),
                                  "thread": name, "seq": n, "scope": scope,
                                  "sources": [{"uri": f"letta-af://{_enc(name)}/messages/{_enc(m.get('id') or n)}"}],
                                  "meta": {"letta_agent": name}}
            if m.get("id"):
                ev["meta"]["letta_message_id"] = m["id"]
            if m.get("created_at"):
                ev["event_time"] = m["created_at"]
            events.append(ev)

        docs: List[Dict[str, Any]] = []
        passages = list(agent.get("passages") or []) + list(agent.get("archival_memory") or [])
        if agent is not data:  # multi-entity layout: top-level passages name their agent
            passages += [x for x in data.get("passages") or []
                         if isinstance(x, dict) and x.get("agent_id") == agent.get("id")]
        for n, p in enumerate(passages):
            text = p.get("text") if isinstance(p, dict) else p if isinstance(p, str) else None
            if not isinstance(text, str) or not text.strip():
                skip("empty_passages")
                continue
            d: Dict[str, Any] = {"kind": "document", "origin": "imported", "content": text, "scope": scope,
                                 "thread": f"{name}/archival",
                                 "sources": [{"uri": f"letta-af://{_enc(name)}/passages/"
                                                     f"{_enc((p.get('id') if isinstance(p, dict) else None) or n)}"}],
                                 "meta": {"letta_agent": name}}
            if isinstance(p, dict) and p.get("created_at"):
                d["event_time"] = p["created_at"]
            docs.append(d)

        prof_ids, d1 = _write(client, profiles, agent_key, "blocks", batch_size, kw) if profiles else ([], 0)
        ev_ids, d2 = _write(client, events, agent_key, "messages", batch_size, kw) if events else ([], 0)
        doc_ids, d3 = _write(client, docs, agent_key, "passages", batch_size, kw) if docs else ([], 0)
        summaries.append({"agent": name, "thread": name, "scope": scope, "profile_ids": prof_ids,
                          "event_ids": ev_ids, "document_ids": doc_ids, "deduplicated": d1 + d2 + d3,
                          "skipped": skipped,
                          "not_imported": [k for k in ("tools", "tool_rules", "llm_config", "embedding_config",
                                                       "system") if agent.get(k)]})
    md = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    return {"format": fmt, "version": data.get("version") or md.get("revision_id"),
            "level": LEVEL, "agents": summaries}


__all__ = ["import_af", "LEVEL"]
