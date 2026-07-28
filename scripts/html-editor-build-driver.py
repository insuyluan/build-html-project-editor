#!/usr/bin/env python3
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath

WORKSPACE = Path(os.environ.get("HTML_EDITOR_WORKSPACE", "/workspace")).resolve()
SOURCE_ZIP = WORKSPACE / "source.zip"
SOURCE_DIR = WORKSPACE / "source"
RESULT_ZIP = WORKSPACE / "result.zip"
MAX_FILES = 20_000
MAX_BYTES = 350 * 1024 * 1024
IGNORED = {
    ".git", "node_modules", ".next", ".nuxt", ".output", ".parcel-cache",
    ".svelte-kit", ".turbo", "coverage", "__MACOSX",
}


def log(message):
    print(f"[html-editor] {message}", flush=True)


def fail(message):
    raise RuntimeError(message)


def safe_archive_path(raw):
    normalized = raw.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        return None
    if any(part in IGNORED for part in path.parts) or ".DS_Store" in path.parts:
        return None
    return path


def restore_executable_mode(destination, info):
    archived_permissions = (info.external_attr >> 16) & 0o777
    executable_bits = archived_permissions & 0o111
    if executable_bits == 0:
        with destination.open("rb") as extracted:
            if extracted.read(2) == b"#!":
                executable_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    if executable_bits == 0:
        return False
    current_mode = stat.S_IMODE(destination.stat().st_mode)
    destination.chmod(current_mode | executable_bits)
    return True


def extract_source():
    if not SOURCE_ZIP.is_file():
        fail("Không tìm thấy source.zip trong Sandbox.")
    shutil.rmtree(SOURCE_DIR, ignore_errors=True)
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    total = 0
    count = 0
    executable_count = 0
    with zipfile.ZipFile(SOURCE_ZIP) as archive:
        for info in archive.infolist():
            path = safe_archive_path(info.filename)
            if path is None or info.is_dir():
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                fail(f"ZIP chứa symbolic link không được phép: {info.filename}")
            count += 1
            total += max(0, info.file_size)
            if count > MAX_FILES:
                fail(f"ZIP vượt quá giới hạn {MAX_FILES} tệp.")
            if total > MAX_BYTES:
                fail("Nội dung sau giải nén vượt quá giới hạn 350 MB.")
            destination = SOURCE_DIR.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            if restore_executable_mode(destination, info):
                executable_count += 1
    if count == 0:
        fail("ZIP không chứa tệp nguồn có thể build.")
    log(f"Đã giải nén {count} tệp · {total / 1024 / 1024:.1f} MB")
    if executable_count:
        log(f"Đã khôi phục quyền thực thi cho {executable_count} tệp script.")

    children = [item for item in SOURCE_DIR.iterdir() if item.name not in IGNORED]
    root_files = [item for item in children if item.is_file()]
    root_dirs = [item for item in children if item.is_dir()]
    return root_dirs[0] if len(root_dirs) == 1 and not root_files else SOURCE_DIR


def package_dependencies(package):
    values = {}
    values.update(package.get("dependencies") or {})
    values.update(package.get("devDependencies") or {})
    return values


def detect_framework(package):
    deps = package_dependencies(package)
    if "next" in deps:
        return ("next-static", "Next.js", ["out"], "next")
    if "@sveltejs/kit" in deps:
        if "@sveltejs/adapter-static" not in deps and "svelte-adapter-static" not in deps:
            fail("SvelteKit cần @sveltejs/adapter-static trước khi build từ xa.")
        return ("sveltekit-static", "SvelteKit", ["build", "dist"], "plain")
    if "vue" in deps:
        return ("vue-static", "Vue", ["dist"], "vite")
    if "svelte" in deps:
        return ("svelte-static", "Svelte", ["dist"], "vite")
    if "react-scripts" in deps or "react-scripts" in str((package.get("scripts") or {}).get("start", "")):
        return ("react-static", "React", ["build", "dist"], "plain")
    if "vite" in deps:
        label = "Vite + React" if "react" in deps else "Vite"
        adapter = "react-static" if "react" in deps else "vite-static"
        return (adapter, label, ["dist"], "vite")
    fail("Chưa có Build Adapter từ xa cho framework trong package.json.")


def package_score(path, package):
    try:
        adapter, _, _, _ = detect_framework(package)
        score = 500 if adapter else 0
    except Exception:
        score = 0
    scripts = package.get("scripts") or {}
    if scripts.get("build"):
        score += 250
    if scripts.get("dev"):
        score += 120
    if scripts.get("start"):
        score += 80
    depth = len(path.relative_to(PROJECT_ROOT).parts) - 1
    if path == PROJECT_ROOT / "package.json":
        score += 30
    return score - depth * 8


def choose_package(root):
    global PROJECT_ROOT
    PROJECT_ROOT = root
    candidates = []
    for path in root.rglob("package.json"):
        relative = path.relative_to(root)
        if len(relative.parts) > 8 or any(part in IGNORED for part in relative.parts):
            continue
        try:
            package = json.loads(path.read_text("utf-8"))
            candidates.append((package_score(path, package), path, package))
        except Exception:
            continue
    if not candidates:
        fail("Không tìm thấy package.json hợp lệ.")
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, path, package = candidates[0]
    scripts = package.get("scripts") or {}
    if not str(scripts.get("build") or "").strip():
        fail('package.json chưa có script "build".')
    adapter, framework, outputs, strategy = detect_framework(package)
    return path.parent, package, adapter, framework, outputs, strategy


def patch_next_config(app_dir):
    candidates = [
        "next.config.mjs", "next.config.js", "next.config.cjs", "next.config.ts"
    ]
    selected = next((app_dir / name for name in candidates if (app_dir / name).is_file()), app_dir / "next.config.mjs")
    source = selected.read_text("utf-8") if selected.is_file() else ""
    if not source.strip():
        selected.write_text(
            "const nextConfig={output:'export',trailingSlash:true,images:{unoptimized:true}};\n"
            "export default nextConfig;\n",
            "utf-8",
        )
        return selected.name

    esm = re.search(r"\bexport\s+default\s+", source)
    common = re.search(r"\bmodule\.exports\s*=\s*", source)
    match = esm or common
    if not match:
        fail(
            f"Không thể chuẩn hóa {selected.name}: cần `export default` "
            "hoặc `module.exports =`."
        )
    expression_source = source[match.end():].strip()
    if not expression_source:
        fail(f"Không thể đọc giá trị export trong {selected.name}.")
    prefix = source[:match.start()]
    wrapper = f"""const __htmlEditorOriginalConfig = {expression_source}

const __htmlEditorNormalizeConfig = value => ({{
  ...(value || {{}}),
  output: 'export',
  trailingSlash: value && typeof value.trailingSlash === 'boolean' ? value.trailingSlash : true,
  images: {{ ...((value && value.images) || {{}}), unoptimized: true }},
}});
const __htmlEditorStaticConfig = typeof __htmlEditorOriginalConfig === 'function'
  ? async (...args) => __htmlEditorNormalizeConfig(await __htmlEditorOriginalConfig(...args))
  : __htmlEditorNormalizeConfig(__htmlEditorOriginalConfig);
"""
    ending = (
        "module.exports = __htmlEditorStaticConfig;\n"
        if common and not esm
        else "export default __htmlEditorStaticConfig;\n"
    )
    source = prefix + wrapper + ending
    selected.write_text(source.rstrip() + "\n", "utf-8")
    return selected.name


def package_manager(root, app_dir, package):
    declared = str(package.get("packageManager") or "")
    match = re.match(r"^(npm|pnpm|yarn|bun)(?:@[^+\s]+)?", declared)
    if match:
        return match.group(1)
    for directory in (root, app_dir):
        if (directory / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (directory / "yarn.lock").exists():
            return "yarn"
        if (directory / "bun.lock").exists() or (directory / "bun.lockb").exists():
            return "bun"
    return "npm"


def run(command, cwd, env):
    log("$ " + " ".join(command))
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line.rstrip(), flush=True)
    code = process.wait()
    if code != 0:
        fail(f"Lệnh {command[0]} thất bại với mã {code}.")


def install_and_build(root, app_dir, package, strategy):
    manager = package_manager(root, app_dir, package)
    install_dir = root if (root / "package.json").is_file() else app_dir
    base_env = dict(os.environ)
    base_env.update({
        "CI": "true",
        "NEXT_TELEMETRY_DISABLED": "1",
        "NUXT_TELEMETRY_DISABLED": "1",
        "ASTRO_TELEMETRY_DISABLED": "1",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
    })
    install_env = dict(base_env)
    install_env.update({
        "NODE_ENV": "development",
        "npm_config_production": "false",
        "npm_config_include": "dev",
    })
    build_env = dict(base_env)
    build_env["NODE_ENV"] = "production"
    if manager in ("pnpm", "yarn"):
        subprocess.run(["corepack", "enable"], cwd=str(install_dir), env=install_env, check=False)

    if manager == "pnpm":
        install = ["pnpm", "install", "--no-frozen-lockfile"]
        build = ["pnpm", "run", "build"]
        if strategy == "vite":
            build += ["--", "--base", "./"]
    elif manager == "yarn":
        install = ["yarn", "install"]
        build = ["yarn", "run", "build"]
        if strategy == "vite":
            build += ["--base", "./"]
    elif manager == "bun":
        install = ["bun", "install", "--no-save"]
        build = ["bun", "run", "build"]
        if strategy == "vite":
            build += ["--", "--base", "./"]
    else:
        install = ["npm", "ci", "--no-audit", "--no-fund"] if (install_dir / "package-lock.json").is_file() else ["npm", "install", "--no-audit", "--no-fund"]
        build = ["npm", "run", "build"]
        if strategy == "vite":
            build += ["--", "--base", "./"]

    try:
        run(install, install_dir, install_env)
    except RuntimeError:
        if manager != "npm":
            raise
        log("Thử lại npm install với chế độ tương thích peer dependency.")
        run(
            ["npm", "install", "--no-audit", "--no-fund", "--legacy-peer-deps"],
            install_dir,
            install_env,
        )
    run(build, app_dir, build_env)
    return manager


def find_output(app_dir, output_names):
    for name in output_names:
        directory = app_dir / name
        if directory.is_dir() and any(directory.rglob("index.html")):
            return name, directory
    fail("Build không tạo output có index.html trong " + ", ".join(output_names) + ".")


def create_result(output_name, output_dir, adapter, framework, source_name):
    files = [path for path in output_dir.rglob("*") if path.is_file()]
    if len(files) >= MAX_FILES:
        fail("Output build tạo quá nhiều tệp.")
    total = sum(path.stat().st_size for path in files)
    if total > MAX_BYTES:
        fail("Output build vượt quá giới hạn 350 MB.")
    entry = next(
        (
            path.relative_to(output_dir).as_posix()
            for path in files
            if path.name.lower() == "index.html"
        ),
        "index.html",
    )
    manifest = {
        "schemaVersion": 1,
        "kind": "html-editor-normalized-build",
        "adapter": adapter,
        "framework": framework,
        "sourceFilename": source_name,
        "outputDirectory": output_name,
        "entryPath": entry,
        "fileCount": len(files) + 1,
        "totalBytes": total,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "builder": os.environ.get("HTML_EDITOR_BUILD_PROVIDER", "remote-build"),
    }
    manifest_path = output_dir / "html-editor-build-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    if RESULT_ZIP.exists():
        RESULT_ZIP.unlink()
    with zipfile.ZipFile(RESULT_ZIP, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(output_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output_dir).as_posix())
    log(f"Đã tạo output {len(files) + 1} tệp · {total / 1024 / 1024:.1f} MB")
    print("HTML_EDITOR_RESULT:" + json.dumps(manifest, ensure_ascii=False), flush=True)


def main():
    source_name_file = WORKSPACE / "source-name.txt"
    source_name = (
        source_name_file.read_text("utf-8").strip()
        if source_name_file.is_file()
        else "source.zip"
    )
    root = extract_source()
    app_dir, package, adapter, framework, outputs, strategy = choose_package(root)
    log(f"Workspace: {root.relative_to(WORKSPACE)} · App: {app_dir.relative_to(root) or Path('.')}")
    log(f"Build Adapter: {framework} · output {' / '.join(outputs)}")
    if strategy == "next":
        selected = patch_next_config(app_dir)
        log(f"Next adapter: {selected} → static export")
    manager = install_and_build(root, app_dir, package, strategy)
    output_name, output_dir = find_output(app_dir, outputs)
    log(f"Package manager: {manager}")
    create_result(output_name, output_dir, adapter, framework, source_name)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"HTML_EDITOR_ERROR:{error}", file=sys.stderr, flush=True)
        sys.exit(1)
