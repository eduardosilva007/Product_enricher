#!/usr/bin/env python3
"""Read-only Octoparse Data Hub MCP discovery. Never starts scraping tasks."""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://mcp-v2.octoparse.com"
KEY_NAMES = ("OCTOPARSE_API_KEY", "OCTOPARSE_KEY", "OCTO_ENGINE_API_KEY",
             "OCTOPARSE_TOKEN", "OCTOPARSE_DATAHUB_API_KEY")


def read_env(path):
    if not path.is_file():
        raise ValueError(f"ENV_FILE_NOT_FOUND: {path}")
    data = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
            continue
        if value[:1] in ("'", '"') and value[-1:] == value[:1]:
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        data[key] = value
    return data


def get_key(env):
    merged = {**env, **os.environ}
    for name in KEY_NAMES:
        if merged.get(name):
            return name, merged[name]
    matches = [k for k, v in merged.items()
               if "OCTOPARSE" in k.upper() and ("KEY" in k.upper() or "TOKEN" in k.upper()) and v]
    if len(matches) == 1:
        return matches[0], merged[matches[0]]
    raise ValueError("OCTOPARSE_KEY_NOT_FOUND_OR_AMBIGUOUS: " + ",".join(matches))


class MCP:
    def __init__(self, key):
        self.key, self.session, self.version, self.request_id = key, None, "2025-03-26", 0

    def send(self, method, params=None, notification=False):
        self.request_id += 1
        body = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = self.request_id
        if params is not None:
            body["params"] = params
        headers = {"Authorization": "Bearer " + self.key,
                   "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": self.version,
                   "User-Agent": "ProductProvidersLab/0.1"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(),
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                self.session = response.headers.get("Mcp-Session-Id", self.session)
                content_type = response.headers.get("Content-Type", "")
                raw = response.read(5_000_000).decode("utf-8-sig", errors="replace")
        except urllib.error.HTTPError as exc:
            # Never print authentication headers.
            raise RuntimeError(f"HTTP_{exc.code}: " + exc.read(500).decode(errors="replace")) from None
        if notification:
            return None
        if "text/event-stream" in content_type:
            messages = []
            for event in re.split(r"\r?\n\r?\n", raw):
                lines = [line[5:].strip() for line in event.splitlines() if line.startswith("data:")]
                if lines:
                    try:
                        messages.append(json.loads("\n".join(lines)))
                    except json.JSONDecodeError:
                        pass
            matches = [m for m in messages if m.get("id") == self.request_id]
            reply = matches[-1] if matches else (messages[-1] if messages else None)
        else:
            reply = json.loads(raw) if raw.strip() else None
        if not reply:
            raise RuntimeError("EMPTY_MCP_RESPONSE")
        if reply.get("error"):
            raise RuntimeError("MCP_ERROR: " + json.dumps(reply["error"], ensure_ascii=False))
        return reply.get("result", {})


def main():
    parser = argparse.ArgumentParser(description="Octoparse MCP read-only discovery")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--search", default="product")
    parser.add_argument("--output-dir", type=Path, default=Path.home() / "ProductEnricher_ProviderLab")
    args = parser.parse_args()
    key_name, key = get_key(read_env(args.env_file))
    print(json.dumps({"phase": "CONFIG", "key_variable": key_name, "key_present": True,
                      "endpoint": ENDPOINT, "extractions": 0}), flush=True)
    mcp = MCP(key)
    result = mcp.send("initialize", {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "product-providers-lab", "version": "0.1"}})
    mcp.version = result.get("protocolVersion", mcp.version)
    print(json.dumps({"phase": "INITIALIZED", "protocol_version": mcp.version,
                      "server_info": result.get("serverInfo", {}), "extractions": 0}), flush=True)
    mcp.send("notifications/initialized", notification=True)
    listing = mcp.send("tools/list")
    tools = listing.get("tools", [])
    print(json.dumps({"phase": "TOOLS", "count": len(tools),
                      "names": [tool.get("name") for tool in tools], "extractions": 0}), flush=True)
    search = next((tool for tool in tools if tool.get("name") == "search_data_apps"), None)
    if not search:
        print("SEARCH_DATA_APPS_NOT_EXPOSED; no extraction attempted")
        return 2
    schema = search.get("inputSchema", {})
    args.output_dir.mkdir(parents=True, exist_ok=True)
    target = args.output_dir / "octoparse_mcp_discovery.json"
    target.write_text(json.dumps({"endpoint": ENDPOINT, "tools": tools},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"phase": "DISCOVERY_SCHEMA", "schema": schema,
                      "saved_to": str(target), "extractions": 0}), flush=True)
    properties = schema.get("properties", {})
    matched = [name for name in ("query", "keyword", "search", "q") if name in properties]
    required = schema.get("required", [])
    if not matched or any(name != matched[0] for name in required):
        print(json.dumps({"phase": "SEARCH_SKIPPED", "reason": "Schema requires other inputs",
                          "extractions": 0}))
        return 0
    found = mcp.send("tools/call", {"name": "search_data_apps",
                                     "arguments": {matched[0]: args.search}})
    target = args.output_dir / "octoparse_mcp_search_result.json"
    target.write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"phase": "SEARCH_COMPLETED", "raw_json_path": str(target),
                      "result_excerpt": json.dumps(found, ensure_ascii=False)[:2500],
                      "extractions": 0}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"result": "FAILED", "error": str(exc)[:1000],
                          "extractions": 0}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
