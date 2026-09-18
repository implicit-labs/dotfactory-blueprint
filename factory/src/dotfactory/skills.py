"""Deterministic resolution for workflow-declared agent skills."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class SkillResolutionError(RuntimeError):
    def __init__(
        self, message: str, *, requested: Iterable[str], missing: Iterable[str] = (),
        resolved: Iterable["ResolvedSkill"] = (),
    ) -> None:
        super().__init__(message)
        self.requested = tuple(requested)
        self.missing = tuple(missing)
        self.resolved = tuple(resolved)


@dataclass(frozen=True)
class ResolvedSkill:
    name: str
    path: str
    entrypoint: str
    content_hash: str
    file_count: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _declared_name(entrypoint: Path) -> str | None:
    try:
        lines = entrypoint.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise SkillResolutionError(
            f"skill entrypoint is not UTF-8: {entrypoint}", requested=(),
        ) from error
    except OSError as error:
        raise SkillResolutionError(
            f"skill entrypoint cannot be read: {entrypoint}: {error}", requested=(),
        ) from error
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, separator, value = line.partition(":")
        if separator and key.strip() == "name":
            return value.strip().strip("\"'")
    return None


def skill_content_hash(root: Path) -> tuple[str, int]:
    try:
        root = root.resolve(strict=True)
        digest = hashlib.sha256()
        files = 0
        for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if candidate.is_symlink():
                target = candidate.resolve(strict=True)
                if not _inside(target, root):
                    raise SkillResolutionError(
                        f"skill content escapes its installed directory: {candidate}",
                        requested=(),
                    )
                if target.is_dir():
                    raise SkillResolutionError(
                        f"skill contains an unsupported directory link: {candidate}",
                        requested=(),
                    )
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(root).as_posix().encode("utf-8")
            content = candidate.read_bytes()
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
            files += 1
    except SkillResolutionError:
        raise
    except (OSError, RuntimeError) as error:
        raise SkillResolutionError(
            f"skill content cannot be read: {root}: {error}", requested=(),
        ) from error
    if files == 0:
        raise SkillResolutionError(
            f"skill directory contains no files: {root}", requested=(),
        )
    return digest.hexdigest(), files


class SkillResolver:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser().resolve()

    def resolve(self, names: Iterable[str]) -> tuple[ResolvedSkill, ...]:
        requested = tuple(dict.fromkeys(names))
        invalid = [name for name in requested if not SKILL_NAME.fullmatch(name)]
        if invalid:
            raise SkillResolutionError(
                "invalid declared skill names: " + ", ".join(invalid),
                requested=requested, missing=invalid,
            )
        resolved = []
        missing = []
        for name in requested:
            installed = self.directory / name
            try:
                root = installed.resolve(strict=True)
            except FileNotFoundError:
                missing.append(name)
                continue
            except (OSError, RuntimeError) as error:
                raise SkillResolutionError(
                    f"declared skill {name} cannot be resolved: {error}",
                    requested=requested, resolved=resolved,
                ) from error
            entrypoint = root / "SKILL.md"
            if not root.is_dir() or not entrypoint.is_file():
                missing.append(name)
                continue
            declared_name = _declared_name(entrypoint)
            if declared_name != name:
                raise SkillResolutionError(
                    f"declared skill {name} has mismatched SKILL.md name "
                    f"{declared_name or '<missing>'}",
                    requested=requested, resolved=resolved,
                )
            try:
                content_hash, file_count = skill_content_hash(root)
            except SkillResolutionError as error:
                raise SkillResolutionError(
                    f"declared skill {name} is invalid: {error}",
                    requested=requested, resolved=resolved,
                ) from error
            resolved.append(ResolvedSkill(
                name=name, path=str(root), entrypoint=str(entrypoint),
                content_hash=content_hash, file_count=file_count,
            ))
        if missing:
            raise SkillResolutionError(
                "declared skills are not installed: " + ", ".join(missing),
                requested=requested, missing=missing, resolved=resolved,
            )
        return tuple(resolved)


def verify_resolved_skill(skill: ResolvedSkill) -> None:
    entrypoint = Path(skill.entrypoint)
    root = Path(skill.path)
    if not entrypoint.is_file():
        raise SkillResolutionError(
            f"declared skill {skill.name} disappeared before presentation",
            requested=(skill.name,), missing=(skill.name,),
        )
    content_hash, file_count = skill_content_hash(root)
    if content_hash != skill.content_hash or file_count != skill.file_count:
        raise SkillResolutionError(
            f"declared skill {skill.name} changed after preparation",
            requested=(skill.name,), resolved=(skill,),
        )
