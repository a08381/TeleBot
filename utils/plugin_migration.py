"""在服务创建前迁移插件默认数据，保留文件名且不覆盖已有目标。"""

from __future__ import annotations

import os
import shutil
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

_SQLITE_SUFFIXES = ("", "-wal", "-shm", "-journal")


class PluginDataMigrationError(RuntimeError):
    """迁移已中止，处理报错中的文件冲突后再启动插件。"""


@contextmanager
def _exclusive_source(path: Path):
    """复制时独占源文件；POSIX 锁主要覆盖活动事务，升级仍需先停旧进程。"""
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        create.restype = wintypes.HANDLE
        # 禁止共享读写，包括空闲 SQLite 连接；允许发布后删除源文件。
        # OPEN_EXISTING 只打开已有文件，不创建空源文件。
        handle = create(str(path), 0x80000000, 0x4, None, 3, 0x80, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            close = kernel.CloseHandle
            close.argtypes = (wintypes.HANDLE,)
            close(handle)
            raise
        with os.fdopen(fd, "rb") as stream:
            yield stream
    else:
        import fcntl

        with path.open("r+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # SQLite 使用 POSIX 字节范围锁，而不是 flock；同时锁住整份文件，
            # 让其他进程的活动 SQLite 事务阻止迁移。此锁不替代停旧进程。
            fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield stream


def _move_files(pairs: list[tuple[Path, Path]]) -> None:
    """完整复制并以不覆盖方式发布目标，随后删除源文件。"""
    if not pairs:
        return
    try:
        with ExitStack() as stack:
            streams = [(source, target, stack.enter_context(_exclusive_source(source)))
                       for source, target in pairs]
            directory = pairs[0][1].parent
            directory.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix=".data-migration-", dir=directory) as temporary:
                staged = []
                for source, target, stream in streams:
                    copy = Path(temporary) / target.name
                    # 暂存目录仅供当前用户使用；复制时保留原文件权限，
                    # 不扩大其他用户对凭据的访问权限。
                    with copy.open("xb") as output:
                        os.chmod(copy, os.fstat(stream.fileno()).st_mode & 0o777)
                        shutil.copyfileobj(stream, output)
                        output.flush()
                        os.fsync(output.fileno())
                    staged.append((copy, target))
                published = []
                try:
                    for copy, target in staged:
                        # 同目录硬链接在 Windows 和 POSIX 上均可原子发布且
                        # 保证不覆盖已有文件；这里不使用 replace()。
                        os.link(copy, target)
                        published.append(target)
                except BaseException:
                    for target in reversed(published):
                        target.unlink()
                    raise
                # 所有目标均完成复制和刷盘后才删除源文件，SQLite 主文件最后删。
                # 清理中途失败会留下可检测的新旧冲突，下次启动不会静默建空库。
                for source, _, _ in reversed(streams):
                    source.unlink()
    except Exception as exc:
        paths = "; ".join(f"{source} -> {target}" for source, target in pairs)
        raise PluginDataMigrationError(
            f"插件数据迁移中止，请关闭占用程序并检查文件；保留已有目标，勿直接删除：{paths}"
        ) from exc


def migrate_default_data(
    config: Any,
    key: str,
    filename: str,
    *,
    sqlite: bool = False,
    legacy_filenames: tuple[str, ...] = (),
) -> str:
    """只迁移默认位置；自定义路径的写法和原有语义保持不变。

    所有源文件迁移成功后才更新配置。WM 旧单账户文件按原文件名归档，
    当前配置仍切换到多账户文件名，不猜测旧账户属于哪个 Telegram 用户。
    """
    directory = config.directory.resolve()
    root = directory.parent.parent
    raw = str(config.get(key) or "")
    canonical = (directory / filename).relative_to(root).as_posix()
    candidate = Path(raw.strip().replace("\\", "/")).expanduser() if raw.strip() else directory / filename
    if not candidate.is_absolute():
        candidate = root / candidate
    aliases = {
        (base / name).resolve()
        for base in (directory.parent, directory)
        for name in (filename, *legacy_filenames)
    }
    if candidate.resolve() not in aliases:
        return raw

    pairs = []
    for name in (filename, *legacy_filenames):
        suffixes = _SQLITE_SUFFIXES if sqlite else ("",)
        family = [(directory.parent / (name + suffix), directory / (name + suffix))
                  for suffix in suffixes]
        sources = [source for source, _ in family if os.path.lexists(source)]
        targets = [target for _, target in family if os.path.lexists(target)]
        for path in (*sources, *targets):
            if path.is_symlink() or not path.is_file():
                raise PluginDataMigrationError(f"插件数据迁移遇到非普通文件：{path}")
        if sources and targets:
            raise PluginDataMigrationError(
                f"插件数据迁移冲突，源和目标均已保留：{sources[0]} / {targets[0]}"
            )
        if sqlite:
            for existing, main in ((sources, family[0][0]), (targets, family[0][1])):
                if existing and main not in existing:
                    raise PluginDataMigrationError(
                        f"SQLite 附属文件存在但缺少主数据库，迁移中止：{main}"
                    )
        pairs.extend((source, target) for source, target in family if source in sources)

    _move_files(pairs)
    if raw != canonical:
        config.set(key, canonical)
    return str(directory / filename)
