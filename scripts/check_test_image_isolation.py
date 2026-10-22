"""Static release gate: non-production MediaMTX builds cannot retag production."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def image_default(value: str) -> tuple[str | None, str]:
    value = value.strip().strip("\"'")
    match = re.fullmatch(r"\$\{([A-Z_]+):-([^}]+)}", value)
    if match:
        return match[1], match[2]
    if "$" in value:
        raise ValueError("MediaMTX image requires an explicit isolated default")
    return None, value


def mediamtx_image(content: str) -> str:
    block = re.search(r"^  mediamtx:\n((?: {4,}.*\n|[ \t]*\n)*)", content, re.MULTILINE)
    if not block:
        raise ValueError("MediaMTX service missing")
    match = re.search(r"^    image:\s*(.+)$", block[1], re.MULTILINE)
    if not match:
        raise ValueError("MediaMTX image missing")
    return match[1]


def check_isolation(production: str, candidates: dict[str, str]) -> dict[str, str]:
    production_variable, production_default = image_default(production)
    production_repository = production_default.rsplit(":", 1)[0]
    defaults = {}
    for source, value in candidates.items():
        variable, default = image_default(value)
        if (
            default == production_default
            or default.rsplit(":", 1)[0] == production_repository
            or (production_variable and variable == production_variable)
        ):
            raise ValueError(f"{source}: non-production MediaMTX image collides with production")
        defaults[source] = default
    return defaults


def main() -> None:
    production = mediamtx_image((ROOT / "docker-compose.server.yml").read_text(encoding="utf-8"))
    candidates = {}
    for pattern in ("compose*.yml", "compose*.yaml", "docker-compose*.yml", "docker-compose*.yaml"):
        for path in ROOT.glob(pattern):
            if path.name == "docker-compose.server.yml":
                continue
            content = path.read_text(encoding="utf-8")
            if re.search(r"^  mediamtx:$", content, re.MULTILINE):
                candidates[path.name] = mediamtx_image(content)
    # Check the explicit MediaMTX image builds in CI/scripts as well as Compose.
    for directory in (ROOT / ".github/workflows", ROOT / "scripts"):
        for path in directory.rglob("*"):
            if not path.is_file() or path.suffix not in {".yml", ".yaml", ".sh", ".ps1"}:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\bdocker\s+build\b", line) and "mediamtx.Dockerfile" in line:
                    tags = re.findall(r"(?:-t|--tag)(?:=|\s+)([^\s]+)", line)
                    if not tags:
                        raise ValueError(
                            f"{path.name}:{number}: MediaMTX build has no auditable tag"
                        )
                    for tag in tags:
                        candidates[f"{path.relative_to(ROOT)}:{number}:{tag}"] = tag
    defaults = check_isolation(production, candidates)
    print(f"TEST_MEDIAMTX_IMAGE={defaults['compose.test.yml']}")
    print(f"PRODUCTION_DEFAULT_MEDIAMTX_IMAGE={image_default(production)[1]}")
    print(f"TEST_MEDIAMTX_TAG_ISOLATED=PASS sources={len(defaults)}")


if __name__ == "__main__":
    main()
