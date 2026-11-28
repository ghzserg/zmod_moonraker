"""Exercise the real file-list endpoint and filesystem without a printer."""
import asyncio
import gc
import importlib.util
import pathlib
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "moonraker", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
package = importlib.util.module_from_spec(spec)
sys.modules["moonraker"] = package
spec.loader.exec_module(package)
from moonraker.components.file_manager import file_manager as fm
from moonraker.common import RequestType, WebRequest
from moonraker.eventloop import EventLoop
from moonraker.server import Server
from moonraker.utils.exceptions import ServerError


def assert_handled_worker_errors(case, errors):
    # Python 3.14 explicitly reports a failed shielded future after its caller
    # was cancelled. Older supported versions consume that exception silently.
    # Accept that intentional diagnostic, but not leaked tasks or callback errors.
    for context in errors:
        case.assertEqual(context["message"], "PermissionError exception in shielded future")
        case.assertIsInstance(context["exception"], PermissionError)
        case.assertTrue(context["future"].done())
        case.assertIs(context["future"].exception(), context["exception"])


class FileListWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "part.gcode").write_text("; local fixture\n")
        self.manager = fm.FileManager.__new__(fm.FileManager)
        self.manager.server = types.SimpleNamespace(error=ServerError)
        self.manager.event_loop = EventLoop()
        self.manager.file_list_lock = asyncio.Lock()
        self.manager.file_paths = {"gcodes": str(self.root)}
        self.manager.full_access_roots = {"gcodes"}
        self.manager.reserved_paths = {}
        self.manager.ignore_dirs = {".git", "ignored"}

    async def request(self, root="gcodes"):
        return await self.manager._handle_filelist_request(
            WebRequest("/server/files/list", {"root": root}, RequestType.GET)
        )

    async def directory(self, path="gcodes", extended=False):
        return await self.manager._handle_directory_request(
            WebRequest("/server/files/directory", {"path": path, "extended": extended}, RequestType.GET)
        )

    async def test_directory_scan_keeps_event_loop_responsive(self):
        original = fm.shutil.disk_usage
        threads = []
        def slow_usage(*args):
            threads.append(threading.get_ident())
            time.sleep(.3)
            return original(*args)
        with mock.patch.object(fm.shutil, "disk_usage", slow_usage):
            start = time.monotonic()
            task = asyncio.create_task(self.directory())
            await asyncio.sleep(.03)
            elapsed = time.monotonic() - start
            result = await task
        self.assertLess(elapsed, .2)
        self.assertNotIn(threading.get_ident(), threads)
        self.assertEqual(result["files"][0]["filename"], "part.gcode")
        self.assertEqual(result["root_info"], {"name": "gcodes", "permissions": "rw"})
        self.assertEqual(set(result["disk_usage"]), {"total", "used", "free"})

    async def test_cancelled_directory_and_file_list_share_one_worker(self):
        original = fm.FileManager._list_directory
        entered, release = threading.Event(), threading.Event()
        timer = threading.Timer(2, release.set)
        timer.start()
        def held(scanner, *args):
            entered.set()
            release.wait(2)
            return original(scanner, *args)
        try:
            with mock.patch.object(fm.FileManager, "_list_directory", held):
                first = asyncio.create_task(self.directory())
                await self.wait_thread(entered)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                second = asyncio.create_task(self.request())
                await asyncio.sleep(.03)
                self.assertFalse(second.done())
                self.assertTrue(self.manager.file_list_lock.locked())
                release.set()
                self.assertEqual((await asyncio.wait_for(second, 2))[0]["path"], "part.gcode")
        finally:
            release.set()
            timer.cancel()
            timer.join()

    async def test_extended_directory_metadata_is_stable_and_fresh(self):
        storage = fm.MetadataStorage.__new__(fm.MetadataStorage)
        storage.metadata = {"part.gcode": {"estimated_time": 12}}
        storage.mddb = {}
        self.manager.gcode_metadata = storage
        original = fm.FileManager._list_directory
        entered, release = threading.Event(), threading.Event()
        timer = threading.Timer(2, release.set)
        timer.start()
        def held(scanner, *args):
            entered.set()
            release.wait(2)
            return original(scanner, *args)
        try:
            with mock.patch.object(fm.FileManager, "_list_directory", held):
                first = asyncio.create_task(self.directory(extended=True))
                await self.wait_thread(entered)
                storage.insert("part.gcode", {"estimated_time": 24})
                release.set()
                old = await first
            self.assertEqual(old["files"][0]["estimated_time"], 12)
            self.assertEqual((await self.directory(extended=True))["files"][0]["estimated_time"], 24)
            self.assertNotIn("estimated_time", (await self.directory())["files"][0])
        finally:
            release.set()
            timer.cancel()
            timer.join()

    async def test_directory_errors_permissions_and_subdirectories(self):
        for path in ["missing", "gcodes/missing", "gcodes/../../etc"]:
            with self.assertRaises(ServerError):
                await self.directory(path)
        self.manager.reserved_paths["fixture"] = (self.root, False)
        with self.assertRaises(ServerError):
            await self.directory()
        self.manager.reserved_paths.clear()
        (self.root / "nested").mkdir()
        (self.root / "nested" / "child.gcode").write_text(";")
        result = await self.directory("gcodes/nested")
        self.assertEqual([v["filename"] for v in result["files"]], ["child.gcode"])
        self.manager.full_access_roots.clear()
        result = await self.directory("gcodes/nested")
        self.assertEqual(result["root_info"]["permissions"], "r")
        self.assertEqual(result["files"][0]["permissions"], "r")

    async def test_directory_worker_error_does_not_block_next_request(self):
        with mock.patch.object(fm.shutil, "disk_usage", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                await self.directory()
        self.assertEqual((await self.directory())["files"][0]["filename"], "part.gcode")
        self.assertEqual((await self.request())[0]["path"], "part.gcode")

    async def wait_thread(self, event):
        deadline = asyncio.get_running_loop().time() + 2
        while not event.is_set():
            self.assertLess(asyncio.get_running_loop().time(), deadline)
            await asyncio.sleep(.005)

    async def test_slow_scan_does_not_stop_event_loop(self):
        original = fm.os.walk
        scan_threads = []
        def slow_walk(*args, **kwargs):
            scan_threads.append(threading.get_ident())
            time.sleep(.3)
            yield from original(*args, **kwargs)
        with mock.patch.object(fm.os, "walk", slow_walk):
            start = time.monotonic()
            task = asyncio.create_task(self.request())
            await asyncio.sleep(.03)
            elapsed = time.monotonic() - start
            result = await task
        self.assertLess(elapsed, .2, "the endpoint starved other event-loop tasks")
        self.assertNotIn(threading.get_ident(), scan_threads)
        self.assertEqual([f["path"] for f in result], ["part.gcode"])

    async def test_cancelled_client_does_not_release_running_worker(self):
        original = fm.os.walk
        entered = threading.Event()
        release = threading.Event()
        timer = threading.Timer(2, release.set)
        timer.start()
        calls = []
        def held_walk(*args, **kwargs):
            calls.append(threading.get_ident())
            if len(calls) == 1:
                entered.set()
                release.wait(2)
            yield from original(*args, **kwargs)
        try:
            with mock.patch.object(fm.os, "walk", held_walk):
                first = asyncio.create_task(self.request())
                await self.wait_thread(entered)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                second = asyncio.create_task(self.request())
                await asyncio.sleep(.05)
                self.assertEqual(len(calls), 1)
                self.assertTrue(self.manager.file_list_lock.locked())
                release.set()
                result = await asyncio.wait_for(second, 2)
                self.assertEqual(len(calls), 2)
                self.assertEqual(result[0]["path"], "part.gcode")
        finally:
            release.set()
            timer.cancel()
            timer.join()

    async def test_cancelled_queued_request_never_starts_scan(self):
        await self.manager.file_list_lock.acquire()
        queued = asyncio.create_task(self.request())
        await asyncio.sleep(.02)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        self.manager.file_list_lock.release()
        self.assertEqual((await self.request())[0]["path"], "part.gcode")

    async def test_worker_failure_releases_lock_and_propagates(self):
        with mock.patch.object(fm.FileManager, "get_path_info", side_effect=PermissionError("fixture")):
            with self.assertRaises(PermissionError):
                await self.request()
        self.assertEqual((await self.request())[0]["path"], "part.gcode")
        with self.assertRaises(ServerError):
            await self.request("missing")
        self.assertEqual((await self.request())[0]["path"], "part.gcode")

    async def test_cancelled_client_and_worker_error_are_drained(self):
        loop = asyncio.get_running_loop()
        errors = []
        old_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        entered, release = threading.Event(), threading.Event()
        timer = threading.Timer(2, release.set)
        timer.start()
        def failing_walk(*args, **kwargs):
            entered.set()
            release.wait(2)
            raise PermissionError("fixture after disconnect")
        try:
            with mock.patch.object(fm.os, "walk", failing_walk):
                request = asyncio.create_task(self.request())
                await self.wait_thread(entered)
                request.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await request
                self.assertTrue(self.manager.file_list_lock.locked())
                release.set()
                deadline = loop.time() + 2
                while self.manager.file_list_lock.locked():
                    self.assertLess(loop.time(), deadline)
                    await asyncio.sleep(.005)
            del request
            gc.collect()
            await asyncio.sleep(0)
            assert_handled_worker_errors(self, errors)
            self.assertEqual((await self.request())[0]["path"], "part.gcode")
        finally:
            release.set()
            timer.cancel()
            timer.join()
            loop.set_exception_handler(old_handler)

    async def test_server_stop_while_directory_worker_is_running(self):
        storage = fm.MetadataStorage.__new__(fm.MetadataStorage)
        storage.metadata = {"part.gcode": {"estimated_time": 12}}
        storage.mddb = {}
        self.manager.gcode_metadata = storage
        self.manager.scheduled_notifications = {}
        self.manager.fs_observer = types.SimpleNamespace(close=mock.Mock())
        original = fm.FileManager._list_directory
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        worker_metadata = []
        timer = threading.Timer(3, release.set)
        timer.start()
        def held(scanner, *args):
            entered.set()
            release.wait(3)
            try:
                result = original(scanner, *args)
                worker_metadata.append(result["files"][0]["estimated_time"])
                return result
            finally:
                finished.set()
        try:
            with mock.patch.object(fm.FileManager, "_list_directory", held):
                request = asyncio.create_task(self.directory(extended=True))
                await self.wait_thread(entered)
                queued = asyncio.create_task(self.request())
                async def close_app():
                    request.cancel()
                    queued.cancel()
                    await asyncio.gather(request, queued, return_exceptions=True)
                server = Server.__new__(Server)
                server.components = {
                    "database": types.SimpleNamespace(close=storage.metadata.clear),
                    "file_manager": self.manager,
                }
                server.moonraker_app = types.SimpleNamespace(close=close_app)
                server.websocket_manager = types.SimpleNamespace(close=mock.AsyncMock())
                server.klippy_connection = types.SimpleNamespace(close=mock.AsyncMock())
                server.event_loop = self.manager.event_loop
                server.app_running_evt = asyncio.Event()
                with mock.patch.object(server.event_loop, "remove_signal_handler"):
                    await asyncio.wait_for(server._stop_server("terminate"), 1)
                self.assertTrue(server.app_running_evt.is_set())
                self.assertEqual(server.exit_reason, "terminate")
                self.assertFalse(server.server_running)
                self.assertTrue(request.cancelled())
                self.assertTrue(queued.cancelled())
                self.assertTrue(self.manager.file_list_lock.locked())
                self.assertFalse(finished.is_set())
                self.assertEqual(storage.metadata, {})
                self.manager.fs_observer.close.assert_called_once_with()
                release.set()
                await self.wait_thread(finished)
                deadline = asyncio.get_running_loop().time() + 2
                while self.manager.file_list_lock.locked():
                    self.assertLess(asyncio.get_running_loop().time(), deadline)
                    await asyncio.sleep(.005)
                self.assertEqual(worker_metadata, [12])
        finally:
            release.set()
            timer.cancel()
            timer.join()

    async def test_snapshot_isolated_from_path_and_permission_updates(self):
        original = fm.os.walk
        entered = threading.Event()
        release = threading.Event()
        timer = threading.Timer(2, release.set)
        timer.start()
        def held_walk(*args, **kwargs):
            entered.set()
            release.wait(2)
            yield from original(*args, **kwargs)
        try:
            with mock.patch.object(fm.os, "walk", held_walk):
                task = asyncio.create_task(self.request())
                await self.wait_thread(entered)
                replacement = self.root / "replacement"
                replacement.mkdir()
                (replacement / "new.gcode").write_text("; new root")
                self.manager.file_paths["gcodes"] = str(replacement)
                self.manager.full_access_roots.clear()
                self.manager.reserved_paths["fixture"] = (self.root, False)
                release.set()
                old = await task
            entry = next(f for f in old if f["path"] == "part.gcode")
            self.assertEqual(entry["permissions"], "rw")
            new = await self.request()
            self.assertEqual([f["path"] for f in new], ["new.gcode"])
            self.assertEqual(new[0]["permissions"], "")
        finally:
            release.set()
            timer.cancel()
            timer.join()

    async def test_filesystem_contract_links_permissions_and_no_stale_cache(self):
        (self.root / "ignored").mkdir()
        (self.root / "ignored" / "hidden.gcode").write_text(";")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "private.gcode").write_text(";")
        (self.root / "nested").mkdir()
        (self.root / "nested" / "B.GCODE").write_text(";")
        (self.root / "nested" / "loop").symlink_to(self.root, target_is_directory=True)
        (self.root / "readme.txt").write_text("not gcode")
        (self.root / "linked.gcode").symlink_to(self.root / "part.gcode")
        expected = self.manager.get_file_list("gcodes", True)
        first = await self.request()
        self.assertEqual(first, expected)
        self.assertEqual([f["path"] for f in first], ["linked.gcode", "nested/B.GCODE", "part.gcode"])
        self.assertEqual(first[0]["permissions"], "r")
        (self.root / "part.gcode").rename(self.root / "moved.gcode")
        second = await self.request()
        self.assertEqual([f["path"] for f in second], ["moved.gcode", "nested/B.GCODE"])
        (self.root / "moved.gcode").unlink()
        self.assertEqual([f["path"] for f in await self.request()], ["nested/B.GCODE"])


class FileListWorkerLoopShutdownTests(unittest.TestCase):
    def test_asyncio_run_drains_scan_after_client_cancel(self):
        # server.main() uses asyncio.run() for each Moonraker server lifetime.
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []
        timer = None
        with tempfile.TemporaryDirectory() as tmp:
            manager = fm.FileManager.__new__(fm.FileManager)
            manager.server = types.SimpleNamespace(error=ServerError)
            manager.file_paths = {"gcodes": tmp}
            manager.full_access_roots = {"gcodes"}
            manager.reserved_paths = {}
            manager.ignore_dirs = set()
            def held_walk(*args, **kwargs):
                entered.set()
                release.wait(2)
                try:
                    raise PermissionError("fixture during loop shutdown")
                finally:
                    finished.set()
            async def main():
                nonlocal timer
                loop = asyncio.get_running_loop()
                loop.set_exception_handler(lambda _loop, context: errors.append(context))
                manager.event_loop = EventLoop()
                manager.file_list_lock = asyncio.Lock()
                task = asyncio.create_task(manager._handle_filelist_request(
                    WebRequest("/server/files/list", {"root": "gcodes"}, RequestType.GET)
                ))
                deadline = loop.time() + 2
                while not entered.is_set():
                    self.assertLess(loop.time(), deadline)
                    await asyncio.sleep(.005)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(finished.is_set())
                timer = threading.Timer(.1, release.set)
                timer.start()
                # Return while the executor still owns a live filesystem scan.
            try:
                with mock.patch.object(fm.os, "walk", held_walk):
                    asyncio.run(main())
                self.assertTrue(finished.is_set())
                gc.collect()
                assert_handled_worker_errors(self, errors)
            finally:
                release.set()
                if timer is not None:
                    timer.cancel()
                    timer.join()


if __name__ == "__main__":
    unittest.main()
