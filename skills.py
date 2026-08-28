"""Safe Hermes skill discovery and GitHub installation for the browser connector."""

import shutil
import threading
from urllib.parse import urlparse


# ponytail: installs are rare; serialize quarantine use unless parallel installs matter.
_INSTALL_LOCK = threading.Lock()


def parse_github_skill_url(value):
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or parsed.query or parsed.fragment:
        raise ValueError("Use a public https://github.com/owner/repo skill URL")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or any(part in {".", ".."} for part in parts):
        raise ValueError("GitHub skill URL must include owner and repository")
    owner, repo = parts[:2]
    repo = repo.removesuffix(".git")
    if not owner or not repo:
        raise ValueError("GitHub skill URL must include owner and repository")
    if len(parts) == 2:
        return f"{owner}/{repo}", ""
    if len(parts) >= 5 and parts[2] == "tree":
        return f"{owner}/{repo}", "/".join(parts[4:]).strip("/")
    raise ValueError("Use a repository root or /tree/<branch>/<skill-folder> URL")


def list_skills():
    from agent.skill_commands import scan_skill_commands

    commands = scan_skill_commands()
    skills = [
        {
            "command": command,
            "name": str(info.get("name") or command.lstrip("/")),
            "description": str(info.get("description") or ""),
        }
        for command, info in sorted(commands.items())
    ]
    try:
        from tools.image_generation_tool import check_image_generation_requirements
        image_generation = bool(check_image_generation_requirements())
    except Exception:
        image_generation = False
    return {"skills": skills, "image_generation": image_generation}


def install_github_skill(url, *, confirm=False):
    with _INSTALL_LOCK:
        bundle = _fetch_bundle(url)
        from agent.skill_commands import scan_skill_commands
        installed_names = {str(info.get("name") or "") for info in scan_skill_commands().values()}
        if bundle.name in installed_names:
            return {
                "installed": True,
                "already_installed": True,
                "name": bundle.name,
                "source": str(bundle.metadata.get("source_url") or url),
            }
        from tools.skills_hub import install_from_quarantine, quarantine_bundle
        from tools.skills_guard import scan_skill_cached, should_allow_install

        quarantine = quarantine_bundle(bundle)
        try:
            scan, provenance = scan_skill_cached(
                quarantine,
                source=bundle.identifier.split("/", 2)[0] + "/" + bundle.identifier.split("/", 2)[1],
                source_url=str(bundle.metadata.get("source_url") or url),
            )
            allowed, reason = should_allow_install(scan)
            preview = {
                "name": bundle.name,
                "source": str(bundle.metadata.get("source_url") or url),
                "verdict": scan.verdict,
                "trust_level": scan.trust_level,
                "finding_count": len(scan.findings),
                "summary": scan.summary,
                "reason": reason,
            }
            if allowed is False:
                return {"installed": False, "blocked": True, **preview}
            if not confirm:
                return {"installed": False, "requires_confirmation": True, **preview}
            install_dir = install_from_quarantine(
                quarantine,
                bundle.name,
                "",
                bundle,
                scan,
                scan_provenance=provenance,
            )
            quarantine = None
            from agent.skill_commands import reload_skills
            reload_skills()
            return {"installed": True, "path": str(install_dir), **preview}
        finally:
            if quarantine:
                shutil.rmtree(quarantine, ignore_errors=True)


def _fetch_bundle(url):
    from tools.skills_hub import (
        GitHubAuth,
        GitHubSource,
        SkillBundle,
        _referenced_support_paths,
    )
    from tools.skills_tool import _parse_frontmatter

    repo, skill_path = parse_github_skill_url(url)
    source = GitHubSource(GitHubAuth())
    if skill_path:
        bundle = source.fetch(f"{repo}/{skill_path}")
        if bundle is None:
            raise ValueError("Could not find a complete SKILL.md bundle at that GitHub path")
        return bundle

    skill_md = source._fetch_file_content(repo, "SKILL.md")
    if not skill_md:
        raise ValueError("The repository root does not contain SKILL.md")
    referenced = _referenced_support_paths(skill_md)
    if referenced is None:
        raise ValueError("SKILL.md contains unsupported or unsafe support-file references")
    tree = source._get_repo_tree(repo)
    if tree is None:
        raise ValueError("Could not read the GitHub repository tree")
    branch, entries = tree
    revision = source._tree_revisions.get(repo) or branch
    entries_by_path = {item.get("path", ""): item for item in entries}
    files = {"SKILL.md": skill_md}
    for optional_license in ("LICENSE", "LICENSE.txt"):
        if optional_license in entries_by_path:
            referenced.add(optional_license)
    for rel_path in sorted(referenced):
        item = entries_by_path.get(rel_path)
        if not item or item.get("type") != "blob" or item.get("mode") == "120000":
            raise ValueError(f"Referenced skill file is missing or unsafe: {rel_path}")
        content = source._fetch_file_bytes(repo, rel_path)
        if content is None:
            raise ValueError(f"Could not download referenced skill file: {rel_path}")
        files[rel_path] = content
    frontmatter, _ = _parse_frontmatter(skill_md)
    name = str(frontmatter.get("name") or repo.split("/", 1)[1]).strip()
    return SkillBundle(
        name=name,
        files=files,
        source="github",
        identifier=f"{repo}/.",
        trust_level="community",
        metadata={
            "source_url": f"https://github.com/{repo}/tree/{revision}",
            "source_revision": revision,
        },
    )
