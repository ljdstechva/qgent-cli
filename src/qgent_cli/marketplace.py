"""Find and install GIS MCP servers and skills from online catalogs.

* MCP servers come from the official MCP Registry
  (registry.modelcontextprotocol.io), whose entries carry install metadata
  (PyPI / npm / OCI packages, or remote URLs) and required settings.
* Skills come from the open skills directory at skills.sh; each skill's
  summary is read from its SKILL.md on GitHub.

Both catalogs' search matches names loosely ("gis" also finds "registry" and
"logistics"), so results are scored for GIS relevance on their name and
description before they are shown. Installing writes the MCP server into
``~/.qgent/config.toml`` or copies the skill folder into
``~/.qgent/plugin/skills`` — nothing is installed without being selected.
"""
import concurrent.futures
import json
import math
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, config

MCP_REGISTRY = "https://registry.modelcontextprotocol.io/v0/servers"
SKILLS_SEARCH = "https://skills.sh/api/search"
RAW = "https://raw.githubusercontent.com"
GITHUB_API = "https://api.github.com"

MCP_QUERIES = ("geo", "gis", "map", "spatial", "gdal", "osm", "earth", "satellite",
               "elevation", "carto", "postgis", "terrain", "flood", "location")
SKILL_QUERIES = ("gis", "qgis", "geospatial", "gdal", "cartography", "remote sensing",
                 "postgis", "geopandas", "raster", "mapping")
CACHE_HOURS = 24
USER_AGENT = f"qgent-cli/{__version__}"

_STRONG = [r"\bq?gis\b", r"gdal", r"\bogr\b", r"postgis", r"geospatial", r"geojson",
           r"shapefile", r"geopackage", r"\braster", r"lidar", r"\bdems?\b",
           r"cartograph(?!y of (code|repo))", r"\bcrs\b", r"\bepsg", r"wgs ?84", r"\bstac\b",
           r"sentinel-?\d", r"landsat", r"remote sensing", r"openstreetmap", r"\bosm\b",
           r"earth engine", r"geopandas", r"rasterio", r"shapely", r"leaflet", r"maplibre",
           r"mapbox", r"arcgis", r"\bh3\b", r"geocod", r"isochrone", r"elevation",
           r"terrain", r"topograph", r"watershed", r"hydrolog", r"flood", r"satellite",
           r"earth observation", r"spatial (analysis|data|join|index|query)",
           r"vector tiles?", r"basemap", r"choropleth", r"geoserver", r"geotiff", r"\bwms\b",
           r"\bwfs\b", r"coordinate reference", r"reproject", r"land ?cover", r"zoning",
           r"geo-?ai", r"geoscience", r"geolog", r"gemgis", r"open-?gis", r"web ?map"]
_WEAK = [r"\bmaps?\b", r"\bmapping\b", r"location", r"coordinates?", r"boundar", r"routing",
         r"latitude", r"longitude", r"geograph", r"hazard", r"earthquake", r"weather",
         r"climate", r"places?\b", r"\bgeo\b", r"spatial", r"parcel", r"address"]
_NEGATIVE = [r"\bseo\b", r"citation", r"chatgpt", r"crawl", r"\bbrand", r"mind ?maps?",
             r"seat ?map", r"georgia", r"payments?", r"insurance", r"mortgage", r"shipping",
             r"logistic", r"\bip (address|geolocation)", r"ip2location", r"\bgists?\b",
             r"transcript", r"codebase", r"source code", r"repositor(y|ies) map",
             r"cloud asset", r"generative engine", r"ai visibility", r"dom\b", r"web pages?",
             r"cycling", r"tesla", r"beach", r"mind-map", r"sourcemap", r"heap ?map",
             r"\brepos?\b", r"coding agents?", r"architecture diagram", r"dependency map",
             r"knowledge (coverage|graph|map)", r"birth data", r"astrolog"]


def rank(score, installs=0, text=""):
    """Relevance (capped, so keyword-stuffed text can't win alone) + popularity.

    QGIS-specific entries get a bonus: QGent is a QGIS agent first.
    """
    bonus = 6 if "qgis" in str(text).lower() else 0
    return min(score, 12) + bonus + 3 * math.log10(int(installs or 0) + 1)


def gis_score(text):
    text = str(text or "").lower()
    score = sum(3 for p in _STRONG if re.search(p, text))
    score += sum(1 for p in _WEAK if re.search(p, text))
    score -= sum(4 for p in _NEGATIVE if re.search(p, text))
    return score


# -- HTTP + cache -------------------------------------------------------------------
def _get(url, timeout=45, headers=None):
    request = urllib.request.Request(url, headers=dict({"User-Agent": USER_AGENT},
                                                       **(headers or {})))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _get_json(url, timeout=45, headers=None):
    return json.loads(_get(url, timeout, headers).decode("utf-8"))


def _cache_path(name):
    return config.path("cache", name)


def _cache_load(name, hours=CACHE_HOURS):
    path = _cache_path(name)
    try:
        if time.time() - os.path.getmtime(path) > hours * 3600:
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _cache_save(name, data):
    path = _cache_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


def _github_headers():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token and shutil.which("gh"):
        try:
            token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                                   timeout=10, check=False).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            token = ""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# -- MCP servers --------------------------------------------------------------------
def _install_options(server):
    """Ways this machine can run the server, best first."""
    options = []
    has = {"uvx": bool(shutil.which("uvx")), "npx": bool(shutil.which("npx")),
           "docker": bool(shutil.which("docker"))}
    for package in server.get("packages") or []:
        kind = package.get("registryType")
        transport = (package.get("transport") or {}).get("type", "stdio")
        if transport != "stdio":
            continue
        need = {"pypi": "uvx", "npm": "npx", "oci": "docker"}.get(kind)
        if need:
            options.append({"kind": kind, "package": package, "available": has[need],
                            "needs": need})
    for remote in server.get("remotes") or []:
        if remote.get("type") in ("streamable-http", "sse", "http"):
            options.append({"kind": "remote", "remote": remote, "available": True})
    order = {"pypi": 0, "npm": 1, "remote": 2, "oci": 3}
    options.sort(key=lambda o: (not o["available"], order.get(o["kind"], 9)))
    return options


def _mcp_item(server):
    name = server.get("name", "")
    text = " ".join([name, server.get("title") or "", server.get("description") or ""])
    options = _install_options(server)
    kinds = sorted({o["kind"] for o in options})
    return {"id": name, "title": server.get("title") or name.split("/")[-1],
            "summary": server.get("description") or "", "version": server.get("version"),
            "website": server.get("websiteUrl") or (server.get("repository") or {}).get("url"),
            "score": gis_score(text), "kinds": kinds,
            "installable": any(o["available"] for o in options), "server": server}


def _registry_query(query):
    url = MCP_REGISTRY + "?" + urllib.parse.urlencode(
        {"search": query, "limit": 100, "version": "latest"})
    data = _get_json(url, timeout=90)
    return [entry.get("server") or {} for entry in data.get("servers") or []]


def search_mcp(query=None, limit=25, refresh=False):
    """GIS-relevant registry servers, best first. ``query`` narrows the search."""
    queries = (query,) if query else MCP_QUERIES
    cache_name = "mcp_" + re.sub(r"\W+", "_", query or "gis") + ".json"
    servers = None if refresh else _cache_load(cache_name)
    errors = []
    if servers is None:
        found = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_registry_query, q): q for q in queries}
            for future in concurrent.futures.as_completed(futures):
                try:
                    for server in future.result():
                        if server.get("name"):
                            found.setdefault(server["name"], server)
                except (OSError, ValueError, urllib.error.URLError) as exc:
                    errors.append(f"{futures[future]}: {exc}")
        servers = list(found.values())
        if servers:
            _cache_save(cache_name, servers)
    items = [_mcp_item(s) for s in servers]
    threshold = 1 if query else 3
    items = [i for i in items if i["score"] >= threshold]
    items.sort(key=lambda i: (not i["installable"], -rank(i["score"], 0, i["summary"] + i["id"]),
                              i["id"]))
    if not items and errors:
        raise MarketplaceError("MCP Registry unreachable: " + "; ".join(errors[:3]))
    return items[:limit]


def server_slug(item):
    base = item["id"].split("/")[-1] or item["id"]
    slug = re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_") or "server"
    return slug if slug not in ("qgis", "qgis_live", "fetch") else slug + "_mcp"


def requirements(item):
    """Settings the user must supply: [{key, kind: env|header|arg, description, secret}]."""
    options = [o for o in _install_options(item["server"]) if o["available"]]
    if not options:
        return None, []
    option = options[0]
    needed = []
    if option["kind"] == "remote":
        for header in option["remote"].get("headers") or []:
            if header.get("isRequired"):
                needed.append({"key": header.get("name"), "kind": "header",
                               "description": header.get("description", ""),
                               "secret": bool(header.get("isSecret", True))})
    else:
        package = option["package"]
        for var in package.get("environmentVariables") or []:
            if var.get("isRequired") and not var.get("default"):
                needed.append({"key": var.get("name"), "kind": "env",
                               "description": var.get("description", ""),
                               "secret": bool(var.get("isSecret"))})
        for arg in package.get("packageArguments") or []:
            if arg.get("isRequired") and not (arg.get("value") or arg.get("default")):
                needed.append({"key": arg.get("name") or arg.get("valueHint") or "value",
                               "kind": "arg", "description": arg.get("description", ""),
                               "secret": bool(arg.get("isSecret")), "arg": arg})
    return option, needed


def mcp_spec(item, values=None):
    """config.toml [mcp_servers.<slug>] entry for the chosen install option."""
    values = values or {}
    option, needed = requirements(item)
    if option is None:
        raise MarketplaceError("no install method available here (needs "
                               + ", ".join(sorted({o.get('needs', o['kind']) for o in
                                                   _install_options(item['server'])}))
                               + ")")
    missing = [n["key"] for n in needed if not values.get(n["key"])]
    if missing:
        raise MarketplaceError("missing required settings: " + ", ".join(missing))
    spec = {"source": f"mcp-registry:{item['id']}@{item.get('version') or ''}"}
    if option["kind"] == "remote":
        remote = option["remote"]
        spec.update(url=remote["url"],
                    transport="sse" if remote.get("type") == "sse" else "http")
        headers = {n["key"]: values[n["key"]] for n in needed if n["kind"] == "header"}
        if headers:
            spec["headers"] = headers
        return spec
    package = option["package"]
    ident, version = package.get("identifier"), package.get("version")
    # Absolute launcher paths: on Windows "npx" alone does not resolve to
    # npx.cmd when Claude/Codex spawn the server.
    if option["kind"] == "pypi":
        spec.update(command=shutil.which("uvx") or "uvx",
                    args=[f"{ident}=={version}" if version else ident])
    elif option["kind"] == "npm":
        spec.update(command=shutil.which("npx") or "npx",
                    args=["-y", f"{ident}@{version}" if version else ident])
    else:
        image = ident if ":" in ident or not version else f"{ident}:{version}"
        env_flags = []
        for var in package.get("environmentVariables") or []:
            env_flags += ["-e", var.get("name")]
        spec.update(command=shutil.which("docker") or "docker",
                    args=["run", "-i", "--rm", *env_flags, image])
    for arg in package.get("packageArguments") or []:
        value = values.get(arg.get("name") or arg.get("valueHint") or "value") \
            or arg.get("value") or arg.get("default")
        if not value:
            continue
        if arg.get("type") == "named" and arg.get("name"):
            spec["args"] += [arg["name"], str(value)]
        else:
            spec["args"].append(str(value))
    env = {n["key"]: values[n["key"]] for n in needed if n["kind"] == "env"}
    for var in package.get("environmentVariables") or []:
        if var.get("default") and var.get("name") not in env and var.get("isRequired"):
            env[var["name"]] = str(var["default"])
    if option["kind"] == "pypi":
        env.setdefault("PYTHONIOENCODING", "utf-8")
    if env:
        spec["env"] = env
    return spec


def install_mcp(item, values=None, name=None):
    cfg = config.load()
    slug = name or server_slug(item)
    spec = mcp_spec(item, values)
    cfg.setdefault("mcp_servers", {})[slug] = spec
    config.save(cfg)
    return slug, spec


def remove_mcp(name):
    cfg = config.load()
    servers = cfg.get("mcp_servers") or {}
    if name not in servers:
        return False
    servers.pop(name)
    config.save(cfg)
    return True


# -- skills -------------------------------------------------------------------------
def user_plugin_dir():
    return config.path("plugin")


def user_skills_dir():
    return os.path.join(user_plugin_dir(), "skills")


def ensure_user_plugin():
    """~/.qgent/plugin is a Claude Code plugin, so installed skills load natively."""
    manifest = os.path.join(user_plugin_dir(), ".claude-plugin", "plugin.json")
    if not os.path.isfile(manifest):
        os.makedirs(os.path.dirname(manifest), exist_ok=True)
        with open(manifest, "w", encoding="utf-8") as handle:
            json.dump({"name": "qgent-user", "version": "1.0.0",
                       "description": "Skills installed with `qgent skills add`."},
                      handle, indent=1)
    os.makedirs(user_skills_dir(), exist_ok=True)
    return user_plugin_dir()


def _skills_query(query):
    data = _get_json(SKILLS_SEARCH + "?" + urllib.parse.urlencode({"q": query}), timeout=30)
    return data.get("skills") or []


def _frontmatter(text):
    from .skills import _frontmatter as parse
    return parse(text)


_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+$")


def _check_source(source):
    """skills.sh data is third-party: only plain owner/repo reaches GitHub
    URLs, which may carry the user's token."""
    if not _REPO.match(str(source or "")) or ".." in source:
        raise MarketplaceError(f"not a GitHub owner/repo: {source!r}")
    return source


def _repo_tree(source, headers):
    _check_source(source)
    cache_name = "tree_" + re.sub(r"\W+", "_", source) + ".json"
    cached = _cache_load(cache_name)
    if cached is not None:
        return cached
    data = _get_json(f"{GITHUB_API}/repos/{source}/git/trees/HEAD?recursive=1",
                     timeout=30, headers=headers)
    tree = [t["path"] for t in data.get("tree") or [] if t.get("type") == "blob"]
    _cache_save(cache_name, tree)
    return tree


def locate_skill(source, skill_id, headers=None):
    """(path of SKILL.md in the repo, its text, the repo file list or None)."""
    _check_source(source)
    if not _SAFE_NAME.match(str(skill_id or "")):
        raise MarketplaceError(f"not a valid skill id: {skill_id!r}")
    for path in (f"skills/{skill_id}/SKILL.md", f"{skill_id}/SKILL.md",
                 f".claude/skills/{skill_id}/SKILL.md"):
        try:
            return path, _get(f"{RAW}/{source}/HEAD/{path}", timeout=20).decode(
                "utf-8", "replace"), None
        except (urllib.error.URLError, OSError):
            continue
    tree = _repo_tree(source, headers or _github_headers())
    candidates = [p for p in tree if p.endswith("SKILL.md")]
    by_folder = [p for p in candidates if p.split("/")[-2:-1] == [skill_id]]
    ordered = by_folder + [p for p in candidates if p not in by_folder]
    for path in ordered[:12]:
        text = _get(f"{RAW}/{source}/HEAD/{path}", timeout=20).decode("utf-8", "replace")
        meta = _frontmatter(text)
        if path in by_folder or meta.get("name") == skill_id or len(candidates) == 1:
            return path, text, tree
    raise MarketplaceError(f"no SKILL.md for {skill_id!r} in {source}")


def _describe(entry, headers):
    try:
        path, text, tree = locate_skill(entry["source"], entry["skillId"], headers)
    except (MarketplaceError, OSError, ValueError, urllib.error.URLError) as exc:
        return dict(entry, summary="", path=None, error=str(exc)[:120])
    meta = _frontmatter(text)
    return dict(entry, summary=meta.get("description", ""), path=path, tree=tree,
                display=meta.get("name") or entry["skillId"])


def search_skills(query=None, limit=25, refresh=False):
    """GIS skills from skills.sh with summaries, best first."""
    queries = (query,) if query else SKILL_QUERIES
    cache_name = "skills_" + re.sub(r"\W+", "_", query or "gis") + ".json"
    cached = None if refresh else _cache_load(cache_name)
    if cached is not None:
        return cached[:limit]
    found, errors = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_skills_query, q): q for q in queries}
        for future in concurrent.futures.as_completed(futures):
            try:
                for skill in future.result():
                    if skill.get("id") and _REPO.match(str(skill.get("source") or "")) \
                            and ".." not in skill["source"]:
                        found.setdefault(skill["id"], skill)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                errors.append(f"{futures[future]}: {exc}")
    if not found and errors:
        raise MarketplaceError("skills.sh unreachable: " + "; ".join(errors[:3]))
    threshold = 1 if query else 3
    # Names first (cheap), then summaries for the plausible ones.
    names = {s["id"]: gis_score(s["id"].replace("-", " ").replace("/", " "))
             for s in found.values()}
    candidates = [s for s in found.values() if names[s["id"]] >= (0 if query else 1)]
    # Most GIS-looking names first, popularity second; summaries are fetched
    # only for these, since each costs GitHub requests.
    candidates.sort(key=lambda s: ("qgis" not in s["id"].lower(), -min(names[s["id"]], 6),
                                   -int(s.get("installs") or 0)))
    candidates = candidates[: limit * 3]
    headers = _github_headers()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        described = list(pool.map(lambda s: _describe(s, headers), candidates))
    items = []
    for skill in described:
        text = " ".join([skill["skillId"].replace("-", " "), skill.get("summary", "")])
        score = gis_score(text)
        if score < threshold or not skill.get("path"):
            continue
        installs = int(skill.get("installs") or 0)
        items.append({"id": skill["id"], "name": skill["skillId"], "source": skill["source"],
                      "installs": installs, "summary": skill["summary"],
                      "path": skill["path"], "score": score,
                      "rank": rank(score, installs, text + " " + skill["id"]),
                      "url": f"https://skills.sh/{skill['id']}"})
    items.sort(key=lambda i: -i["rank"])
    if not query:
        # A package of 30 sibling skills shouldn't fill the whole list.
        per_repo, kept = {}, []
        for item in items:
            per_repo[item["source"]] = per_repo.get(item["source"], 0) + 1
            if per_repo[item["source"]] <= 3:
                kept.append(item)
        items = kept
    _cache_save(cache_name, items)
    return items[:limit]


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*$")


def skill_dir(name):
    """The folder for skill ``name`` — refusing anything that could leave the
    skills folder (``..``, separators, drive letters)."""
    name = str(name or "")
    root = os.path.realpath(user_skills_dir())
    target = os.path.realpath(os.path.join(root, name))
    if (not _SAFE_NAME.match(name) or os.path.dirname(target) != root):
        raise MarketplaceError(f"not a valid skill name: {name!r}")
    return target


def _installed_id(target):
    try:
        with open(os.path.join(target, ".qgent-source.json"), encoding="utf-8") as handle:
            return json.load(handle).get("id")
    except (OSError, ValueError):
        return None


def install_skill(item, max_files=80, max_bytes=5_000_000):
    """Copy the skill's folder from GitHub into ~/.qgent/plugin/skills/<name>."""
    ensure_user_plugin()
    _check_source(item["source"])
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", item["name"]).strip("-") or "skill"
    existing = _installed_id(skill_dir(name))
    if existing and existing != item["id"]:
        # A different skill already owns this folder name: keep both.
        owner = re.sub(r"[^A-Za-z0-9_-]+", "-", item["source"].split("/")[0])
        name = f"{name}-{owner}"
    folder = item["path"].rsplit("/", 1)[0] if "/" in item["path"] else ""
    try:
        tree = _repo_tree(item["source"], _github_headers())
        files = [p for p in tree if folder and p.startswith(folder + "/")] or [item["path"]]
    except (OSError, ValueError, urllib.error.URLError):
        files = [item["path"]]
    if len(files) > max_files:
        raise MarketplaceError(f"{len(files)} files is more than a skill should have "
                               f"(limit {max_files}); not installed")
    target = skill_dir(name)
    staging = target + ".partial"
    shutil.rmtree(staging, ignore_errors=True)
    total = 0
    for path in files:
        rel = path[len(folder) + 1:] if folder else os.path.basename(path)
        parts = rel.split("/")
        if any(part in ("", ".", "..") or ":" in part or "\\" in part for part in parts):
            continue
        data = _get(f"{RAW}/{item['source']}/HEAD/{urllib.parse.quote(path)}", timeout=30)
        total += len(data)
        if total > max_bytes:
            shutil.rmtree(staging, ignore_errors=True)
            raise MarketplaceError("skill is larger than 5 MB; not installed")
        dest = os.path.join(staging, *parts)
        if not os.path.realpath(dest).startswith(os.path.realpath(staging) + os.sep):
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(data)
    if not os.path.isfile(os.path.join(staging, "SKILL.md")):
        shutil.rmtree(staging, ignore_errors=True)
        raise MarketplaceError("download did not include SKILL.md")
    with open(os.path.join(staging, ".qgent-source.json"), "w", encoding="utf-8") as handle:
        json.dump({"id": item["id"], "source": item["source"], "path": item["path"],
                   "url": item.get("url"), "installed": time.strftime("%Y-%m-%d %H:%M"),
                   "files": len(files)}, handle, indent=1)
    shutil.rmtree(target, ignore_errors=True)
    os.replace(staging, target)
    return name, target, len(files)


def remove_skill(name):
    """Uninstall one user skill; False if there is no such skill."""
    try:
        target = skill_dir(name)
    except MarketplaceError:
        return False
    if not os.path.isfile(os.path.join(target, "SKILL.md")):
        return False
    shutil.rmtree(target)
    return True


class MarketplaceError(Exception):
    pass
