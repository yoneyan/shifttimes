#!/usr/bin/env python3
"""依存ライブラリの更新だけを含む変更かを判定し、パッチリリース（vX.Y.Z の Z を上げる）の可否を決める.

auto-patch-release.yaml から呼び出す。前回の安定版タグ（vX.Y.Z）から HEAD までの変更について、
次をすべて満たすときに限り「自動でパッチリリースしてよい」と判定する。

1. 変更ファイルが依存定義（uv.lock / pyproject.toml）と、アプリに影響しないファイル
   （.github/ 配下・docs/ 配下・Markdown など）だけである
2. 本番で使う依存（dev グループ以外）のバージョンが実際に変わっている
3. どの依存もメジャーバージョンが上がっていない（0.x 系はマイナーの変化も破壊的とみなす）。
   ダウングレードも自動では扱わない

DB マイグレーションの有無は、前回タグの DB に対する ``migrate --check`` でワークフロー側が確認する
（ライブラリ側が同梱するマイグレーションも検出するため）。

判定結果は GITHUB_OUTPUT（release / prev_tag / tag / notes）と GITHUB_STEP_SUMMARY に書き出す。
ローカルでは ``python3 .github/scripts/auto_patch_release.py`` で判定内容だけを確認できる。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

STABLE_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

# 依存定義のファイル。ここだけが変わった場合に自動リリースの対象になる
DEPENDENCY_FILES = {"uv.lock", "pyproject.toml"}

# アプリ（Docker イメージ）の中身に影響しないファイル。変わっていても自動リリースを妨げない
NON_RUNTIME_PATTERNS = [
    ".github/*",
    "docs/*",
    "*.md",
    ".claude/*",
    ".gitignore",
    ".git-blame-ignore-revs",
    ".hadolint.yaml",
]

# 年や年.月をメジャーバージョンにする（CalVer）ライブラリ。メジャーの変化を破壊的変更とみなさない。
# 4 桁の年（2026.1 など）は自動で CalVer と判定するので、ここには 2 桁の年を使うものを並べる
CALVER_PACKAGES = {
    "attrs",
    "autobahn",
    "automat",
    "constantly",
    "incremental",
    "packaging",
    "pyopenssl",
    "service-identity",
    "twisted",
    "txaio",
}


@dataclass
class Change:
    name: str
    old: str | None
    new: str | None
    kind: str
    breaking: bool


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def parse_tag(tag: str) -> tuple[int, int, int] | None:
    m = STABLE_TAG_RE.match(tag)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def latest_stable_tag() -> str | None:
    """HEAD から辿れる安定版タグ（プレリリースを除く vX.Y.Z）のうち最も新しいものを返す."""
    tags = [t for t in run("git", "tag", "--list", "v*", "--merged", "HEAD").split() if parse_tag(t)]
    return max(tags, key=parse_tag) if tags else None


def changed_files(base: str) -> list[str]:
    return [f for f in run("git", "diff", "--name-only", base, "HEAD").splitlines() if f]


def is_non_runtime(path: str) -> bool:
    return any(fnmatch(path, pattern) for pattern in NON_RUNTIME_PATTERNS)


def export_runtime_deps(project_dir: Path) -> dict[str, str]:
    """uv.lock から本番で入る依存（dev グループ以外）を {"名前 ; マーカー": バージョン} で返す."""
    out = run(
        "uv",
        "export",
        "--frozen",
        "--all-groups",
        "--no-group",
        "dev",
        "--no-emit-project",
        "--no-hashes",
        "--no-header",
        "--no-annotate",
        "--project",
        str(project_dir),
    )
    deps: dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        requirement, _, marker = line.partition(";")
        name, _, version = requirement.partition("==")
        key = name.strip().lower()
        if marker.strip():
            key += f" ; {marker.strip()}"
        deps[key] = version.strip()
    return deps


def export_runtime_deps_at(rev: str) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("pyproject.toml", "uv.lock"):
            Path(tmp, name).write_text(run("git", "show", f"{rev}:{name}"))
        return export_runtime_deps(Path(tmp))


def release_tuple(version: str) -> tuple[int, ...] | None:
    m = re.match(r"^(\d+(?:\.\d+)*)", version)
    return tuple(int(x) for x in m[1].split(".")) if m else None


def classify(key: str, old: str, new: str) -> tuple[str, bool]:
    """バージョンの変化の種別と、それが破壊的変更（自動リリースしない）かを返す."""
    o, n = release_tuple(old), release_tuple(new)
    if o is None or n is None:
        return "不明", True
    o_pad, n_pad = o + (0,) * (3 - len(o)), n + (0,) * (3 - len(n))
    if n_pad < o_pad:
        return "ダウングレード", True
    name = key.split(" ;")[0]
    if name in CALVER_PACKAGES or (o[0] >= 1000 and n[0] >= 1000):
        return "更新 (CalVer)", False
    if n_pad[0] != o_pad[0]:
        return "メジャー", True
    if n_pad[1] != o_pad[1]:
        # 0.x 系はマイナーの変化が破壊的変更になり得る（SemVer の慣例）
        return "マイナー", o_pad[0] == 0
    return "パッチ", False


def diff_deps(old: dict[str, str], new: dict[str, str]) -> list[Change]:
    changes = []
    for key in sorted(old.keys() | new.keys()):
        o, n = old.get(key), new.get(key)
        if o == n:
            continue
        if o is None:
            changes.append(Change(key, None, n, "追加", False))
        elif n is None:
            changes.append(Change(key, o, None, "削除", False))
        else:
            kind, breaking = classify(key, o, n)
            changes.append(Change(key, o, n, kind, breaking))
    return changes


def changes_table(changes: list[Change]) -> str:
    rows = ["| パッケージ | 変更前 | 変更後 | 種別 |", "|---|---|---|---|"]
    for c in changes:
        kind = f"**{c.kind}**" if c.breaking else c.kind
        rows.append(f"| `{c.name}` | {c.old or '-'} | {c.new or '-'} | {kind} |")
    return "\n".join(rows)


def write_github_file(env_name: str, text: str) -> None:
    path = os.environ.get(env_name)
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def set_output(name: str, value: str) -> None:
    if "\n" in value:
        delimiter = f"EOF_{uuid.uuid4().hex}"
        write_github_file("GITHUB_OUTPUT", f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
    else:
        write_github_file("GITHUB_OUTPUT", f"{name}={value}\n")


def finish(release: bool, message: str, *, prev_tag: str = "", tag: str = "", details: str = "") -> int:
    # release でもこの後の DB マイグレーション確認で取りやめることがあるため「対象」と表現する
    title = f"自動パッチリリースの対象: {tag}" if release else "自動パッチリリースは行いません"
    summary = f"## {title}\n\n{message}\n"
    if details:
        summary += f"\n{details}\n"
    print(summary)
    write_github_file("GITHUB_STEP_SUMMARY", summary)
    set_output("release", "true" if release else "false")
    set_output("prev_tag", prev_tag)
    set_output("tag", tag)
    set_output("notes", details if release else "")
    return 0


def main() -> int:
    prev_tag = latest_stable_tag()
    if prev_tag is None:
        return finish(False, "基準となる安定版タグ（vX.Y.Z）がありません。最初のタグは手動で作成してください。")

    files = changed_files(prev_tag)
    if not files:
        return finish(False, f"{prev_tag} から変更がありません。", prev_tag=prev_tag)

    others = [f for f in files if f not in DEPENDENCY_FILES and not is_non_runtime(f)]
    if others:
        listing = "\n".join(f"- `{f}`" for f in others)
        return finish(
            False,
            f"{prev_tag} 以降に依存ライブラリ以外の変更が含まれるため、"
            "バージョンは手動で決めてタグを作成してください。",
            prev_tag=prev_tag,
            details=listing,
        )

    if not DEPENDENCY_FILES & set(files):
        return finish(False, f"{prev_tag} 以降にアプリへ影響する変更がありません。", prev_tag=prev_tag)

    try:
        old_deps = export_runtime_deps_at(prev_tag)
        new_deps = export_runtime_deps(Path.cwd())
    except subprocess.CalledProcessError as e:
        print(e.stderr, file=sys.stderr)
        message = "依存ライブラリの一覧を取得できませんでした。手動でリリースしてください。"
        return finish(False, message, prev_tag=prev_tag)

    changes = diff_deps(old_deps, new_deps)
    if not changes:
        return finish(
            False,
            f"{prev_tag} 以降、本番で使う依存ライブラリのバージョンは変わっていません（開発用の依存のみの更新など）。",
            prev_tag=prev_tag,
        )

    table = changes_table(changes)
    if any(c.breaking for c in changes):
        return finish(
            False,
            "メジャーバージョンの更新など、破壊的変更の可能性がある依存の更新が含まれます（太字）。"
            "動作を確認したうえで、バージョンを手動で決めてタグを作成してください。",
            prev_tag=prev_tag,
            details=table,
        )

    major, minor, patch = parse_tag(prev_tag)
    tag = f"v{major}.{minor}.{patch + 1}"
    notes = (
        f"## 依存ライブラリの更新\n\n{prev_tag} からの依存ライブラリの更新のみを含む自動パッチリリースです。\n\n{table}"
    )
    message = f"{prev_tag} 以降の変更は依存ライブラリの更新のみです。"
    return finish(True, message, prev_tag=prev_tag, tag=tag, details=notes)


if __name__ == "__main__":
    sys.exit(main())
