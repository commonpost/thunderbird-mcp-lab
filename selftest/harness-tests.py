#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Guillaume Laforest
"""Unit tests of the end-to-end lab harness (harness/lab.py): everything that needs neither Thunderbird, mailpit nor
Docker. They raise the measured coverage of the harness (selftest/coverage.sh) and pin its behaviour: input header and
scenario reading, mbox and MIME analysis, probes, the MCP bridge client, the profile and XPI stages, the checks, the
result block and the process-level error paths.

Usage: python3 selftest/harness-tests.py    (from any directory; exit status 0 = every test passed)

lab.py is loaded from its path without running its __main__ block. Its module state (result, process table, paths) is
replaced for every test by a temporary tree, so tests are independent; processes and network are faked, except a
loopback TCP server for labHttpIdle and the harness run as a subprocess for its input error paths (these need a
writable /tmp).
"""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import socket
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import types
import unittest
import urllib.request
import zipfile
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LAB = os.path.join(ROOT, "harness", "lab.py")


def load_lab():
    spec = importlib.util.spec_from_file_location("lab_under_test", LAB)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lab = load_lab()
REAL_LOG = lab.log
REAL_LOGS = lab.LOGS
NONCE = "0123456789abcdef" * 2
GECKO_ID = "commonpost-mcp@commonpost.github.io"
SCENARIO = {"name": "n", "description": "d", "steps": [{"id": "s1", "tool": "listFolders"}],
            "checks": [{"type": "tool_ok", "step": "s1"}]}


def eml(headers, body=""):
    """A raw message with CRLF line ends, the way Thunderbird saves drafts."""
    return "".join(h + "\r\n" for h in headers) + "\r\n" + body


def mime(boundary, *parts):
    """A multipart body: every part is a ready-made string (its headers, a blank line, its body)."""
    return "".join("--%s\r\n%s\r\n" % (boundary, p) for p in parts) + "--%s--\r\n" % boundary


def nested(levels, leaf="Content-Type: text/plain\r\n\r\nleaf"):
    """A part that is a multipart containing a multipart ... (levels deep) containing the leaf."""
    part = leaf
    for n in range(levels, 0, -1):
        part = 'Content-Type: multipart/mixed; boundary="n%d"\r\n\r\n%s' % (n, mime("n%d" % n, part))
    return part


def attachment_draft(subject, name, payload, cte="base64"):
    att = ('Content-Type: application/octet-stream; name="%s"\r\nContent-Disposition: attachment; filename="%s"\r\n'
           "Content-Transfer-Encoding: %s\r\n\r\n%s" % (name, name, cte, payload))
    return eml(["Subject: " + subject, 'Content-Type: multipart/mixed; boundary="B1"'],
               mime("B1", "Content-Type: text/plain\r\n\r\nhi", att))


def ok_reply(text):
    return {"result": {"content": [{"type": "text", "text": text}]}}


def result_block(text, nonce=NONCE):
    begin = lab.MARK_BEGIN % nonce + "\n"
    start = text.index(begin) + len(begin)
    return json.loads(text[start:text.index("\n" + lab.MARK_END % nonce + "\n", start)])


class FakeInput:
    """Stands in for the os module of lab.py: os.read(0, n) serves a buffer, at most `step` bytes per call."""

    def __init__(self, data, step=65536):
        self.data, self.pos, self.step = data, 0, step

    def read(self, fd, n):
        assert fd == 0
        chunk = self.data[self.pos:self.pos + min(n, self.step)]
        self.pos += len(chunk)
        return chunk


class FakeProc:
    """Stands in for the Popen of the bridge or of Thunderbird (code None = still running)."""

    def __init__(self, out=b"", code=0, wait_error=None):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(out)
        self.code, self.wait_error, self.signals = code, wait_error, []

    def poll(self):
        return self.code

    def send_signal(self, sig):
        self.signals.append(sig)

    def wait(self, timeout=None):
        if self.wait_error:
            raise self.wait_error
        return self.code


class BlockingOut:
    """A bridge stdout that never delivers a line until released."""

    def __init__(self):
        self.release = threading.Event()

    def readline(self, limit):
        self.release.wait(10)
        return b""


class FakeBridge:
    """The part of Bridge that run_mcp and run_group use. Tool replies are canned: one response, or a list of them (the
    n-th attempt gets the n-th, the last one repeats)."""

    def __init__(self, replies=None, rpc=None):
        self.replies, self.rpc = replies or {}, rpc or {}
        self.requests, self.sent, self.calls, self.waits, self.notes, self.attempts = [], [], [], [], [], {}

    def request(self, method, params, timeout):
        self.requests.append((method, params, timeout))
        return self.rpc[method]

    def send(self, obj):
        self.sent.append(obj)

    def send_request(self, method, params):
        self.calls.append((method, params))
        return len(self.calls)

    def wait_responses(self, rids, timeout):
        self.waits.append((list(rids), timeout))
        out = {}
        for rid in rids:
            name = self.calls[rid - 1][1]["name"]
            reply = self.replies[name]
            if isinstance(reply, list):
                n = self.attempts.get(name, 0)
                self.attempts[name] = n + 1
                reply = reply[min(n, len(reply) - 1)]
            out[rid] = (reply, 1.5)
        return out


class OneShotServer:
    """A TCP server on 127.0.0.1 for one connection: it reads once, then does what `mode` says (close, reply, reset,
    hold until released)."""

    def __init__(self, mode, reply=b""):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.mode, self.reply, self.received = mode, reply, b""
        self.release = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            try:
                self.received = conn.recv(65536)
            except OSError:
                return
            if self.mode == "reply":
                conn.sendall(self.reply)
            elif self.mode == "reset":
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            elif self.mode == "hold":
                self.release.wait(10)

    def close(self):
        self.release.set()
        self.sock.close()
        self.thread.join(5)


class LabCase(unittest.TestCase):
    """Fresh module state of lab.py in a temporary tree for every test; log lines are kept in self.logs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.logs = []
        patcher = mock.patch.multiple(
            lab, R={"harness": lab.HARNESS, "ok": False, "errors": [], "phases": {}}, PROCS={}, TOOLS={},
            FULLTEXT={}, FILES_FULL={}, MAILPIT_FULL=[], PW=None, NONCE="", SCENARIO_RAW=b"",
            DEADLINE=time.monotonic() + 560, LAB=self.tmp + "/lab", SRC=self.tmp + "/lab/src",
            PROF=self.tmp + "/lab/profile", LOGS=self.tmp + "/logs", CONN=self.tmp + "/conn.json",
            log=self.logs.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.makedirs(lab.LOGS)

    def put(self, path, data=b""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def put_drafts(self, *messages):
        data = b"".join(b"From - Thu Jan 01 00:00:00 2026\r\n" + m.encode("utf-8") + b"\r\n" for m in messages)
        return self.put(lab.PROF + "/Mail/Local Folders/Drafts", data)

    def kinds(self, probe_result):
        return [o["status"] for o in probe_result["occurrences"]]


# ------------------------------------------------------------------------------------------------ input and process
class InputTests(LabCase):
    def test_read_line_stops_at_the_newline_and_leaves_the_rest(self):
        src = FakeInput(b"abc\nrest")
        with mock.patch.object(lab, "os", src):
            self.assertEqual(lab._read_line(10), b"abc")
            self.assertEqual(lab._read_line(10), b"rest")
            self.assertEqual(lab._read_line(10), b"")

    def test_read_line_gives_up_on_a_line_longer_than_the_limit(self):
        with mock.patch.object(lab, "os", FakeInput(b"abcdef\n")):
            self.assertIsNone(lab._read_line(3))
        with mock.patch.object(lab, "os", FakeInput(b"abc\n")):
            self.assertEqual(lab._read_line(3), b"abc")

    def test_read_header_takes_the_nonce_then_the_scenario_and_leaves_the_tar_stream(self):
        raw = json.dumps(SCENARIO).encode()
        data = NONCE.encode() + b"\n" + str(len(raw)).encode() + b"\n" + raw + b"TAR-STREAM"
        src = FakeInput(data, step=7)
        with mock.patch.object(lab, "os", src), mock.patch.object(lab, "make_nondumpable", return_value=True):
            lab.read_header()
        self.assertEqual(lab.NONCE, NONCE)
        self.assertEqual(lab.SCENARIO_RAW, raw)
        self.assertIs(lab.R["nondumpable"], True)
        self.assertEqual(src.data[src.pos:], b"TAR-STREAM")

    def test_read_header_accepts_an_empty_scenario(self):
        with mock.patch.object(lab, "os", FakeInput(NONCE.encode() + b"\n0\n")), \
                mock.patch.object(lab, "make_nondumpable", return_value=False):
            lab.read_header()
        self.assertEqual(lab.SCENARIO_RAW, b"")
        self.assertIs(lab.R["nondumpable"], False)

    def test_read_header_stops_the_process_on_a_bad_nonce(self):
        for data in (b"", b"abc\n", NONCE.upper().encode() + b"\n", NONCE.encode()[:31] + b"\n",
                     b"0" * 70 + b"\n", NONCE.encode() + b"x\n"):
            with self.subTest(data=data[:20]), mock.patch.object(lab, "os", FakeInput(data)), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as cm:
                    lab.read_header()
            self.assertEqual(cm.exception.code, 2)
            self.assertEqual(out.getvalue(), "missing or invalid nonce: stopping\n")
        self.assertEqual(lab.NONCE, "")

    def test_read_header_rejects_a_bad_length_line(self):
        for tail in (b"abc\n", b"\n", b"", b"12345678\n", b"1000001\n", b"-5\n", b"99999999999\n"):
            with self.subTest(tail=tail), mock.patch.object(lab, "os", FakeInput(NONCE.encode() + b"\n" + tail)), \
                    mock.patch.object(lab, "make_nondumpable", return_value=True):
                with self.assertRaisesRegex(ValueError, "scenario length line missing or invalid"):
                    lab.read_header()

    def test_read_header_rejects_input_that_ends_inside_the_scenario(self):
        with mock.patch.object(lab, "os", FakeInput(NONCE.encode() + b"\n10\nabc")), \
                mock.patch.object(lab, "make_nondumpable", return_value=True):
            with self.assertRaisesRegex(ValueError, "input ended inside the scenario"):
                lab.read_header()

    def test_make_nondumpable_reports_what_the_kernel_confirms(self):
        class Libc:
            def __init__(self, dumpable):
                self.dumpable, self.calls = dumpable, []

            def prctl(self, *args):
                self.calls.append(args)
                return self.dumpable if args[0] == 3 else 0

        for dumpable, expected in ((0, True), (1, False)):
            libc = Libc(dumpable)
            with mock.patch("ctypes.CDLL", return_value=libc):
                self.assertIs(lab.make_nondumpable(), expected)
            self.assertEqual(libc.calls, [(4, 0, 0, 0, 0), (3, 0, 0, 0, 0)])

    def test_make_nondumpable_is_false_when_libc_cannot_be_used(self):
        with mock.patch("ctypes.CDLL", side_effect=OSError("no libc")):
            self.assertIs(lab.make_nondumpable(), False)


class Exited(Exception):
    pass


class InitTests(LabCase):
    def fake_os(self, pid=1, fork=0, waits=()):
        waits = list(waits)

        def wait():
            if not waits:
                raise ChildProcessError()
            return waits.pop(0)

        def _exit(status):
            raise Exited(status)
        return types.SimpleNamespace(getpid=lambda: pid, fork=lambda: fork, wait=wait, _exit=_exit,
                                     waitstatus_to_exitcode=os.waitstatus_to_exitcode)

    def run_init(self, **kw):
        with mock.patch.object(lab, "os", self.fake_os(**kw)), \
                mock.patch.object(lab, "make_nondumpable") as nondumpable:
            try:
                return lab.run_as_init(), nondumpable.call_count
            except Exited as e:
                return e.args[0], nondumpable.call_count

    def test_it_does_nothing_when_it_is_not_pid_1(self):
        self.assertEqual(self.run_init(pid=42), (False, 0))

    def test_the_forked_child_continues_as_the_harness(self):
        self.assertEqual(self.run_init(fork=0), (True, 1))

    def test_the_init_exits_with_the_status_of_the_harness_after_reaping_orphans(self):
        self.assertEqual(self.run_init(fork=55, waits=[(7, 0), (55, 3 << 8)]), (3, 1))

    def test_the_init_reports_a_killed_harness_as_128_plus_the_signal(self):
        self.assertEqual(self.run_init(fork=55, waits=[(55, 9)]), (137, 1))

    def test_the_init_fails_when_there_is_no_child_left_to_wait_for(self):
        self.assertEqual(self.run_init(fork=55), (1, 1))


class UtilityTests(LabCase):
    def test_clip_keeps_short_text_and_marks_a_truncation(self):
        self.assertIsNone(lab.clip(None))
        self.assertEqual(lab.clip("abc", 3), "abc")
        self.assertEqual(lab.clip("abcdef", 3), "abc...[truncated: 6 characters in total]")

    def test_left_is_bounded_by_the_cap_the_deadline_and_half_a_second(self):
        with mock.patch.object(lab, "DEADLINE", time.monotonic() + 3):
            self.assertAlmostEqual(lab.left(100), 3, delta=0.5)
            self.assertEqual(lab.left(1), 1)
            self.assertEqual(lab.left(0.1), 0.5)
        with mock.patch.object(lab, "DEADLINE", time.monotonic() - 10):
            self.assertEqual(lab.left(100), 0.5)

    def test_log_writes_a_timestamped_line_to_stderr(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            REAL_LOG("hello")
        self.assertRegex(err.getvalue(), r"^\[lab +\d+\.\ds\] hello\n$")

    def test_phase_records_the_info_with_a_timestamp(self):
        lab.phase("demo", a=1, b="x")
        info = lab.R["phases"]["demo"]
        self.assertEqual((info["a"], info["b"]), (1, "x"))
        self.assertIsInstance(info["t"], float)
        self.assertTrue(self.logs[0].startswith("demo: {"))

    def test_fail_records_and_logs_the_error(self):
        lab.fail("it broke")
        self.assertEqual(lab.R["errors"], ["it broke"])
        self.assertEqual(self.logs, ["ERROR: it broke"])

    def test_sha256_of_a_file_and_of_a_missing_file(self):
        path = self.put(self.tmp + "/f", b"abc")
        self.assertEqual(lab.sha256(path), hashlib.sha256(b"abc").hexdigest())
        self.assertIsNone(lab.sha256(self.tmp + "/missing"))

    def test_tail_returns_the_last_lines_and_clips_the_characters(self):
        path = self.put(self.tmp + "/log", "".join("line %d\n" % i for i in range(100)))
        self.assertEqual(lab.tail(path, nlines=3), "line 97\nline 98\nline 99")
        self.assertEqual(lab.tail(path, nlines=3, nchars=4), "line...[truncated: 23 characters in total]")
        self.assertIsNone(lab.tail(self.tmp + "/missing"))

    def test_tail_only_looks_at_the_last_200000_bytes(self):
        path = self.put(self.tmp + "/big", b"first\n" + b"x" * 300000 + b"\nlast\n")
        self.assertEqual(lab.tail(path, nlines=1), "last")
        self.assertLessEqual(len(lab.tail(path, nlines=1000, nchars=10 ** 6)), 200000)

    def test_start_runs_a_process_in_its_own_session_with_its_log(self):
        p = lab.start("hello", [sys.executable, "-I", "-c", "import os; print('hi', os.getsid(0) == os.getpid())"])
        self.assertIs(lab.PROCS["hello"], p)
        self.assertEqual(p.wait(30), 0)
        with open(lab.LOGS + "/hello.log") as f:
            self.assertEqual(f.read(), "hi True\n")

    def test_start_sends_stdout_to_the_given_pipe(self):
        p = lab.start("piped", [sys.executable, "-I", "-c", "print('out')"], stdout=subprocess.PIPE)
        self.assertEqual(p.communicate(timeout=30)[0], b"out\n")

    def test_stop_kills_the_process_group_and_ignores_the_rest(self):
        lab.start("sleeper", [sys.executable, "-I", "-c", "import time; time.sleep(60)"])
        lab.stop("sleeper")
        self.assertEqual(lab.PROCS["sleeper"].wait(30), -9)
        lab.stop("sleeper")                 # already exited
        lab.stop("never-started")

    def test_stop_uses_the_given_signal(self):
        lab.start("sleeper", [sys.executable, "-I", "-c", "import time; time.sleep(60)"])
        lab.stop("sleeper", lab.signal.SIGTERM)
        self.assertEqual(lab.PROCS["sleeper"].wait(30), -15)

    def test_stop_survives_a_process_group_that_is_gone(self):
        lab.PROCS["ghost"] = types.SimpleNamespace(pid=999999, poll=lambda: None)
        with mock.patch.object(lab.os, "killpg", side_effect=ProcessLookupError) as killpg:
            lab.stop("ghost")
        killpg.assert_called_once_with(999999, lab.signal.SIGKILL)

    def test_wait_for_returns_the_first_true_value(self):
        values = iter([0, None, "found", "later"])
        self.assertEqual(lab.wait_for(lambda: next(values), 5, 0.01), "found")

    def test_wait_for_ignores_exceptions_of_the_predicate_until_the_timeout(self):
        calls = []

        def pred():
            calls.append(1)
            raise OSError("not yet")
        with mock.patch.object(lab, "left", return_value=0.05):
            self.assertIsNone(lab.wait_for(pred, 5, 0.01))
        self.assertGreaterEqual(len(calls), 2)

    def test_http_and_http_json_go_through_the_opener_without_a_proxy(self):
        seen = {}

        class Opener:
            def open(self, req, timeout=None):
                seen.update(method=req.get_method(), url=req.full_url, timeout=timeout)
                return contextlib.nullcontext(types.SimpleNamespace(status=200, read=lambda n: b'{"a": [1]}'[:n]))
        with mock.patch.object(lab, "HTTP", Opener()):
            self.assertEqual(lab.http("DELETE", "http://127.0.0.1:1/x", 7), (200, b'{"a": [1]}'))
            self.assertEqual(seen, {"method": "DELETE", "url": "http://127.0.0.1:1/x", "timeout": 7})
            self.assertEqual(lab.http_json("http://127.0.0.1:1/y"), {"a": [1]})
            self.assertEqual((seen["method"], seen["timeout"]), ("GET", 10))
        # build_opener(ProxyHandler({})) leaves no proxy handler: the proxy variables of the environment are ignored.
        self.assertEqual([h for h in lab.HTTP.handlers if isinstance(h, urllib.request.ProxyHandler)], [])


# ------------------------------------------------------------------------------------------------ scenario
class ScenarioTests(LabCase):
    def test_expand_args_replaces_a_generator_and_records_its_notes(self):
        notes = []
        out = lab.expand_args({"a": [{"$lab_base64": {"bytes": 3}}], "b": {"$lab_base64": {"bytes": 2, "byte": 0}},
                               "c": 5}, "", notes)
        self.assertEqual(out, {"a": ["YWFh"], "b": "AAA=", "c": 5})
        self.assertEqual(notes, [
            {"path": "a[0]", "bytes": 3, "byte": 97, "base64_length": 4, "sha256": hashlib.sha256(b"aaa").hexdigest()},
            {"path": "b", "bytes": 2, "byte": 0, "base64_length": 4, "sha256": hashlib.sha256(b"\0\0").hexdigest()}])

    def test_expand_args_without_notes_and_in_validation_mode(self):
        obj = {"x": {"$lab_base64": {"bytes": 4}}}
        self.assertEqual(lab.expand_args(obj, "", None), {"x": "YWFhYQ=="})
        self.assertEqual(lab.expand_args(obj, "", None, generate=False), obj)

    def test_expand_args_rejects_malformed_generators(self):
        bad = [{"bytes": 0}, {"bytes": lab.MAX_GEN + 1}, {"bytes": True}, {"bytes": "3"}, {"bytes": 3, "byte": 256},
               {"bytes": 3, "byte": -1}, {"bytes": 3, "extra": 1}, "text", {}]
        for spec in bad:
            with self.subTest(spec=spec), self.assertRaisesRegex(ValueError, r"\$lab_base64 = \{bytes 1-"):
                lab.expand_args({"k": {"$lab_base64": spec}}, "", None, generate=False)

    def test_expand_args_names_the_path_of_the_bad_generator(self):
        with self.assertRaisesRegex(ValueError, r"^a\.b\[1\]: "):
            lab.expand_args({"a": {"b": [1, {"$lab_base64": {"bytes": 0}}]}}, "", None)

    def test_pref_line_formats_booleans_integers_and_strings(self):
        self.assertEqual(lab.pref_line("a.b", True), 'user_pref("a.b", true);\n')
        self.assertEqual(lab.pref_line("a.b", False), 'user_pref("a.b", false);\n')
        self.assertEqual(lab.pref_line("n", 1), 'user_pref("n", 1);\n')
        self.assertEqual(lab.pref_line("s", 'q"\u00e9'), 'user_pref("s", "q\\"\\u00e9");\n')

    def test_load_scenario_accepts_a_minimal_scenario(self):
        sc = lab.load_scenario(json.dumps(SCENARIO))
        self.assertEqual(sc["steps"][0]["id"], "s1")

    def test_load_scenario_takes_bridge_first_as_a_boolean_or_seconds(self):
        cold = {"type": "cold_start", "answered": True}
        for value in (True, False, 0, 10, 60):
            with self.subTest(value=value):
                lab.load_scenario(json.dumps(dict(SCENARIO, bridge_first=value)))
        for value in ("during", 61, -1, 1.5, None, [1]):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "^bridge_first: true, false or seconds"):
                lab.load_scenario(json.dumps(dict(SCENARIO, bridge_first=value)))
        lab.load_scenario(json.dumps(dict(SCENARIO, bridge_first=5, checks=[cold])))
        for sc in (dict(SCENARIO, checks=[cold]), dict(SCENARIO, bridge_first=False, checks=[cold])):
            with self.assertRaisesRegex(ValueError, 'check cold_start: needs "bridge_first"'):
                lab.load_scenario(json.dumps(sc))

    def test_load_scenario_refuses_more_than_5_mb_of_seed_messages(self):
        sc = dict(SCENARIO, seed=[{"folder": "127.0.0.1/Inbox", "messages": ["x" * 260000] * 20}])
        with self.assertRaisesRegex(ValueError, "seed: at most 5 MB"):
            lab.load_scenario(json.dumps(sc))
        sc["seed"][0]["messages"] = ["x" * 250000] * 20
        lab.load_scenario(json.dumps(sc))

    def test_validate_files_reports_valid_invalid_and_missing_files(self):
        good = self.put(self.tmp + "/good.json", json.dumps(dict(SCENARIO, checks=[
            {"type": "tool_ok", "step": "s1"}, {"type": "tool_ok", "step": "s1", "xfail": "known"}])))
        bad = self.put(self.tmp + "/bad.json", "{}")
        big = self.put(self.tmp + "/big.json", b" " * (lab.MAX_SCENARIO + 1))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(lab.validate_files([good]), 0)
            self.assertEqual(lab.validate_files([good, bad]), 1)
            self.assertEqual(lab.validate_files([big, self.tmp + "/missing.json"]), 1)
            self.assertEqual(lab.validate_files([]), 1)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "ok       %s (1 steps, 2 checks, 1 xfail)" % good)
        self.assertEqual(lines[2], "INVALID  %s: checks: a list of 1 to 200 checks" % bad)
        self.assertEqual(lines[3], "INVALID  %s: larger than 1000000 bytes" % big)
        self.assertTrue(lines[4].startswith("INVALID  %s: [Errno 2]" % (self.tmp + "/missing.json")))


# ------------------------------------------------------------------------------------------------ mbox and MIME
class MboxTests(LabCase):
    def test_split_mbox_starts_a_message_at_a_thunderbird_from_line_or_after_a_blank_line(self):
        data = (b"junk before the first message\n"
                b"From - Thu Jan 01 00:00:00 2026\r\nA: 1\r\n\r\nbody\r\nFrom stays in the body\r\n"
                b"From - Fri Jan 02 00:00:00 2026\r\nB: 2\r\n\r\n\r\nFrom after a blank line\r\nlast")
        msgs = lab.split_mbox(data)
        self.assertEqual([m["from_line"] for m in msgs], [
            b"From - Thu Jan 01 00:00:00 2026\r\n", b"From - Fri Jan 02 00:00:00 2026\r\n",
            b"From after a blank line\r\n"])
        self.assertEqual(msgs[0]["lines"], [b"A: 1\r\n", b"\r\n", b"body\r\n", b"From stays in the body\r\n"])
        self.assertEqual(msgs[2]["lines"], [b"last"])

    def test_split_mbox_of_nothing_is_empty(self):
        self.assertEqual(lab.split_mbox(b""), [])
        self.assertEqual(lab.split_mbox(b"no from line\n"), [])

    def test_analyse_message_splits_headers_and_body_and_flags_bare_cr(self):
        m = {"from_line": b"From - X\n", "lines": [
            b"Subject: Hello\r\n", b" world\r\n", b"X-Bare: a\rb\r\n", b"not a header\r\n", b"\r\n", b"Body text\r\n"]}
        a = lab.analyse_message(m)
        self.assertEqual(a["subject"], "Hello world")
        self.assertEqual(a["body"], b"Body text\r\n")
        self.assertEqual([f["name"] for f in a["fields"]], ["Subject", "X-Bare", None])
        self.assertEqual(a["fields"][0]["cont"], [b" world\r\n"])
        self.assertEqual(a["view"]["header_names"], ["Subject", "X-Bare", None])
        self.assertEqual(a["view"]["lines_with_bare_cr"], ["X-Bare: a\rb\r\n"])
        self.assertEqual(a["view"]["from_line"], "From - X\n")
        self.assertEqual(a["view"]["body_excerpt"], "Body text\r\n")
        self.assertEqual(a["view"]["header_lines"][1], " world\r\n")
        self.assertEqual(a["view"]["raw_headers"], "".join(l.decode() for l in a["hdr"]))

    def test_analyse_message_without_a_body_or_subject(self):
        a = lab.analyse_message({"from_line": b"", "lines": [b"X-Only: 1\n"]})
        self.assertEqual((a["subject"], a["body"]), ("", b""))
        self.assertEqual(lab.analyse_message({"from_line": b"", "lines": []})["view"]["header_names"], [])

    def test_analyse_message_caps_the_views(self):
        lines = [b"X-H%d: v\r\n" % i for i in range(250)] + [b"\r\n", b"b" * 3000]
        view = lab.analyse_message({"from_line": b"", "lines": lines})["view"]
        self.assertEqual(len(view["header_lines"]), 200)
        self.assertEqual(len(view["header_names"]), 250)
        self.assertTrue(view["body_excerpt"].startswith("b" * 2000 + "...[truncated: 3000"))

    def test_parse_raw_handles_a_last_line_without_a_newline(self):
        a = lab.parse_raw(b"Subject: s\r\nX: y\r\n\r\nbody\r\nlast")
        self.assertEqual(a["subject"], "s")
        self.assertEqual(a["body"], b"body\r\nlast")
        self.assertEqual(a["view"]["from_line"], "")
        self.assertEqual(lab.parse_raw(b"Subject: s\n\nbody\n")["body"], b"body\n")

    def test_unfold_joins_continuations_and_tolerates_a_missing_colon(self):
        a = lab.parse_raw(b"X-Long: one\r\n two\r\n\tthree\r\nno colon here\r\n\r\n")
        self.assertEqual(lab.unfold(a["fields"][0]), "one two\tthree")
        self.assertEqual(lab.unfold(a["fields"][1]), "no colon here")

    def test_header_values_are_case_insensitive_and_keep_every_occurrence(self):
        a = lab.parse_raw(b"Received: one\r\nreceived: two\r\nX-Other: 3\r\nbroken line\r\n\r\n")
        self.assertEqual(lab.header_values(a, "RECEIVED"), ["one", "two"])
        self.assertEqual(lab.header_values(a, "Absent"), [])
        self.assertEqual(lab.header_values(a, 5), [])

    def test_mbox_files_lists_regular_mailbox_files_only(self):
        mail = lab.PROF + "/Mail"
        for rel in ("Local Folders/Inbox", "Local Folders/Drafts", "Local Folders/Drafts.sbd/Sub",
                    "Local Folders/Inbox.msf", "Local Folders/msgFilterRules.dat", "Local Folders/a.json",
                    "Local Folders/filterlog.html"):
            self.put("%s/%s" % (mail, rel))
        os.symlink(mail + "/Local Folders/Inbox", mail + "/Local Folders/Linked")
        self.assertEqual(lab.mbox_files(), sorted(mail + "/Local Folders/" + n for n in (
            "Drafts", "Drafts.sbd/Sub", "Inbox")))

    def test_drafts_come_from_files_named_drafts_with_their_folder(self):
        self.put_drafts(eml(["Subject: One"], "b1"), eml(["Subject: Two"], "b2"))
        self.put(lab.PROF + "/Mail/Local Folders/Inbox", b"From - Thu Jan 01 00:00:00 2026\nSubject: Not a draft\n\n")
        ds = lab.drafts()
        self.assertEqual([d["subject"] for d in ds], ["One", "Two"])
        self.assertEqual({d["folder"] for d in ds}, {"Mail/Local Folders/Drafts"})
        self.assertEqual([len(m) for m in (lab.read_mbox(lab.PROF + "/Mail/Local Folders/Inbox"),)], [1])


class ProbeTests(LabCase):
    def probe(self, headers, body="", header="X-Probe", needle="needle"):
        return lab.probe(lab.parse_raw(eml(headers, body).encode()), header, needle)

    def test_a_standalone_header_line_is_own_header_whatever_else_matches(self):
        r = self.probe(["Subject: hi", "X-Probe: has needle here", "X-Other: needle too"], "body needle")
        self.assertEqual(r["status"], "OWN_HEADER")
        self.assertEqual(self.kinds(r), ["OWN_HEADER", "EMBEDDED", "IN_BODY"])
        self.assertEqual(r["occurrences"][0], {"status": "OWN_HEADER", "carrier": "X-Probe",
                                               "line": "X-Probe: has needle here\r\n"})
        self.assertEqual((r["header"], r["needle"]), ("X-Probe", "needle"))

    def test_the_header_name_is_matched_case_insensitively_across_its_continuation_lines(self):
        r = self.probe(["x-probe: first", " needle on a continuation"])
        self.assertEqual((r["status"], r["occurrences"][0]["carrier"]), ("OWN_HEADER", "x-probe"))

    def test_a_continuation_line_of_another_header_is_folded(self):
        r = self.probe(["Subject: first", " needle in the continuation"])
        self.assertEqual(r["status"], "FOLDED")
        self.assertEqual(r["occurrences"], [{"status": "FOLDED", "carrier": "Subject",
                                             "line": " needle in the continuation\r\n"}])

    def test_the_first_line_of_another_header_is_embedded(self):
        r = self.probe(["Subject: a needle in the subject"])
        self.assertEqual((r["status"], r["occurrences"][0]["carrier"]), ("EMBEDDED", "Subject"))

    def test_a_line_that_is_not_a_header_is_embedded_under_a_question_mark(self):
        r = self.probe(["needle without a colon"])
        self.assertEqual((r["status"], r["occurrences"][0]["carrier"]), ("EMBEDDED", "?"))

    def test_text_found_only_after_decoding_rfc2047_is_encoded(self):
        r = self.probe(["Subject: =?utf-8?B?c2VjcmV0IG5lZWRsZQ==?="])
        self.assertEqual(r["status"], "RFC2047_ENCODED")
        self.assertEqual(r["occurrences"][0]["line"], "Subject: =?utf-8?B?c2VjcmV0IG5lZWRsZQ==?=\r\n")

    def test_an_undecodable_or_unrelated_encoded_word_is_absent(self):
        self.assertEqual(self.probe(["Subject: =?x-unknown-charset?B?aGVsbG8=?="])["status"], "ABSENT")
        self.assertEqual(self.probe(["Subject: =?utf-8?B?aGVsbG8=?="])["status"], "ABSENT")

    def test_text_only_in_the_body_is_in_body(self):
        r = self.probe(["Subject: hi"], "some needle")
        self.assertEqual((r["status"], r["occurrences"]), ("IN_BODY", [{"status": "IN_BODY", "carrier": None,
                                                                         "line": None}]))

    def test_nothing_found_is_absent(self):
        r = self.probe(["Subject: hi"], "body")
        self.assertEqual((r["status"], r["occurrences"]), ("ABSENT", []))

    def test_the_most_severe_status_wins(self):
        self.assertEqual(self.probe(["Subject: x needle", "X-A: y", " needle"])["status"], "FOLDED")
        self.assertEqual(self.probe(["Subject: x needle"], "needle")["status"], "IN_BODY")
        self.assertEqual(self.probe(["X-A: =?utf-8?B?c2VjcmV0IG5lZWRsZQ==?=", "Subject: needle"])["status"],
                         "EMBEDDED")

    def test_at_most_ten_occurrences_are_listed(self):
        r = self.probe(["X-A%d: needle" % i for i in range(12)])
        self.assertEqual((r["status"], len(r["occurrences"])), ("EMBEDDED", 10))


class MimeTests(LabCase):
    def message(self, body, ctype='multipart/mixed; boundary="b1"'):
        return lab.parse_raw(eml(["Subject: m", "Content-Type: " + ctype], body).encode())

    def test_mime_parts_splits_the_raw_body_on_the_boundary_and_keeps_the_crs(self):
        msg = self.message("preamble\r\n" + mime("b1", "Content-Type: text/plain\r\n\r\nhello",
                                                 "Content-Type: text/html\r\n\r\n<p>x</p>") + "epilogue\r\n")
        parts = lab.mime_parts(msg)
        self.assertEqual([lab.header_values(p, "Content-Type") for p in parts], [["text/plain"], ["text/html"]])
        self.assertEqual([p["body"] for p in parts], [b"hello\r\n", b"<p>x</p>\r\n"])

    def test_mime_parts_reads_an_unquoted_boundary_from_a_folded_header(self):
        msg = lab.parse_raw(eml(["Content-Type: multipart/mixed;", " BOUNDARY=plain-b; charset=x"],
                                mime("plain-b", "X: 1\r\n\r\na", "X: 2\r\n\r\nb")).encode())
        self.assertEqual([p["body"] for p in lab.mime_parts(msg)], [b"a\r\n", b"b\r\n"])

    def test_mime_parts_is_empty_without_a_boundary(self):
        self.assertEqual(lab.mime_parts(lab.parse_raw(eml(["Subject: x"], "body").encode())), [])
        self.assertEqual(lab.mime_parts(self.message("body", "text/plain")), [])

    def test_mime_parts_keeps_a_last_part_that_is_not_closed(self):
        msg = self.message("--b1\r\nX: 1\r\n\r\nfirst\r\n--b1\r\nX: 2\r\n\r\nsecond\r\n")
        self.assertEqual([p["body"] for p in lab.mime_parts(msg)], [b"first\r\n", b"second\r\n"])

    def test_mime_parts_stops_at_the_closing_delimiter_and_caps_at_30_parts(self):
        body = mime("b1", *["X: %d\r\n\r\np%d" % (i, i) for i in range(32)]) + "--b1\r\nX: 99\r\n\r\nlate\r\n"
        msg = self.message(body)
        parts = lab.mime_parts(msg)
        self.assertEqual(len(parts), 30)
        self.assertEqual(parts[-1]["body"], b"p29\r\n")

    def test_part_report_names_nested_parts_and_applies_the_probes_to_their_headers(self):
        alt = 'Content-Type: multipart/alternative; boundary="alt"\r\n\r\n' + mime(
            "alt", "Content-Type: text/plain\r\nX-Mark: needle\r\n\r\nplain", "Content-Type: text/html\r\n\r\nhtml")
        msg = self.message(mime("b1", alt, "Content-Type: image/png\r\n\r\npng"))
        rep = lab.part_report(msg, [{"header": "X-Mark", "needle": "needle"}])
        self.assertEqual([r["part"] for r in rep], ["1", "1.1", "1.2", "2"])
        self.assertEqual(rep[1]["header_names"], ["Content-Type", "X-Mark"])
        self.assertEqual(rep[1]["header_lines"], ["Content-Type: text/plain\r\n", "X-Mark: needle\r\n"])
        self.assertEqual([r["probes"][0]["status"] for r in rep], ["IN_BODY", "OWN_HEADER", "ABSENT", "ABSENT"])
        self.assertEqual(rep[0]["lines_with_bare_cr"], [])

    def test_part_report_and_all_parts_go_three_levels_deep(self):
        msg = self.message(mime("b1", nested(4)))
        self.assertEqual([r["part"] for r in lab.part_report(msg, [])], ["1", "1.1", "1.1.1"])
        self.assertEqual(len(lab.all_parts(msg)), 3)
        flat = self.message(mime("b1", "X: 1\r\n\r\na", "X: 2\r\n\r\nb"))
        self.assertEqual([p["body"] for p in lab.all_parts(flat)], [b"a\r\n", b"b\r\n"])

    def test_part_report_is_capped_at_30_entries(self):
        group = 'Content-Type: multipart/mixed; boundary="g%d"\r\n\r\n%s'
        msg = self.message(mime("b1", *[group % (g, mime("g%d" % g, *["X: 1\r\n\r\nx"] * 20)) for g in (1, 2)]))
        rep = lab.part_report(msg, [])
        self.assertEqual((len(rep), rep[-1]["part"]), (30, "2.8"))


# ------------------------------------------------------------------------------------------------ bridge
class BridgeTests(LabCase):
    def bridge(self, out=b"", code=0, extra_env=None, stdout=None):
        proc = FakeProc(out, code)
        if stdout is not None:
            proc.stdout = stdout
        with mock.patch.object(lab, "start", return_value=proc) as start:
            br = lab.Bridge(extra_env)
        br.start_call = start.call_args
        return br, proc

    def test_the_bridge_is_started_in_the_source_tree_with_a_minimal_environment(self):
        br, proc = self.bridge()
        args, kw = br.start_call
        self.assertEqual(args, ("bridge", ["node", lab.SRC + "/mcp-bridge.cjs"]))
        self.assertEqual(kw["cwd"], lab.SRC)
        self.assertEqual(sorted(kw["env"]), ["HOME", "LANG", "PATH", "TMPDIR", "TZ"])
        self.assertEqual(kw["stdin"], subprocess.PIPE)
        self.assertNotIn("bridge", lab.R["phases"])
        self.assertIs(br.p, proc)

    def test_bridge_env_variables_reach_the_bridge_only_and_are_recorded(self):
        br, _ = self.bridge(extra_env={"THUNDERBIRD_MCP_B": "2", "COMMONPOST_MCP_A": "1"})
        env = br.start_call[1]["env"]
        self.assertEqual((env["COMMONPOST_MCP_A"], env["THUNDERBIRD_MCP_B"]), ("1", "2"))
        self.assertEqual(list(lab.R["phases"]["bridge"]["env"]), ["COMMONPOST_MCP_A", "THUNDERBIRD_MCP_B"])

    def test_requests_are_numbered_and_written_as_json_lines(self):
        br, proc = self.bridge()
        self.assertEqual(br.send_request("tools/list", {}), 1)
        self.assertEqual(br.send_request("ping", {"x": 1}), 2)
        br.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        lines = [json.loads(l) for l in proc.stdin.getvalue().decode().splitlines()]
        self.assertEqual(lines, [{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                                 {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {"x": 1}},
                                 {"jsonrpc": "2.0", "method": "notifications/initialized"}])

    def test_request_returns_the_matching_response(self):
        br, _ = self.bridge(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}).encode() + b"\n")
        self.assertEqual(br.request("ping", {}, 5), {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

    def test_wait_responses_collects_a_batch_and_keeps_everything_else_as_notes(self):
        lines = [b"not json at all\n", json.dumps({"method": "notifications/x"}).encode() + b"\n", b"[1, 2]\n",
                 json.dumps({"id": 2, "result": "two"}).encode() + b"\n",
                 json.dumps({"id": 2, "result": "again"}).encode() + b"\n",
                 json.dumps({"id": 1, "result": "one"}).encode() + b"\n"]
        br, _ = self.bridge(b"".join(lines))
        got = br.wait_responses([1, 2], 5)
        self.assertEqual({k: v[0]["result"] for k, v in got.items()}, {1: "one", 2: "two"})
        self.assertTrue(all(isinstance(v[1], float) for v in got.values()))
        self.assertEqual(br.notes, ["not json at all\n", '{"method": "notifications/x"}', "[1, 2]",
                                    '{"id": 2, "result": "again"}'])

    def test_wait_responses_reports_a_bridge_that_exited(self):
        br, _ = self.bridge(json.dumps({"id": 1, "result": "one"}).encode() + b"\n", code=7)
        got = br.wait_responses([1, 2, 3], 5)
        self.assertEqual(got[1][0]["result"], "one")
        self.assertEqual((got[2], got[3]), (({"_bridge_exit": 7}, None),) * 2)

    def test_wait_responses_times_out_with_a_marker(self):
        out = BlockingOut()
        self.addCleanup(out.release.set)
        br, _ = self.bridge(stdout=out)
        with mock.patch.object(lab, "left", return_value=0.2):
            self.assertEqual(br.wait_responses([1], 5), {1: ({"_timeout": True}, None)})

    def test_wait_responses_gives_up_at_once_when_the_time_is_over(self):
        br, _ = self.bridge(json.dumps({"id": 1, "result": "one"}).encode() + b"\n")
        with mock.patch.object(lab, "left", return_value=0):
            self.assertEqual(br.wait_responses([1], 5), {1: ({"_timeout": True}, None)})

    def test_notifications_are_kept_with_their_arrival_time_and_as_notes(self):
        lines = [json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}).encode() + b"\n",
                 json.dumps({"id": 9, "method": "roots/list"}).encode() + b"\n",
                 json.dumps({"id": 1, "result": "one"}).encode() + b"\n"]
        br, _ = self.bridge(b"".join(lines))
        br.wait_responses([1], 5)
        self.assertEqual([n["method"] for n in br.notifications], ["notifications/tools/list_changed"])
        self.assertIsInstance(br.notifications[0]["at"], float)
        self.assertEqual(len(br.notes), 2, "a request from the bridge (with an id) is a note, not a notification")

    def test_wait_notification_returns_the_arrival_time_of_the_first_match(self):
        lines = [b"junk\n", json.dumps({"method": "notifications/other"}).encode() + b"\n",
                 json.dumps({"method": "notifications/tools/list_changed"}).encode() + b"\n"]
        br, _ = self.bridge(b"".join(lines))
        at = br.wait_notification("notifications/tools/list_changed", 5)
        self.assertEqual(at, br.notifications[-1]["at"])
        self.assertEqual(br.notes[0], "junk\n")
        self.assertEqual(br.wait_notification("notifications/other", 5), br.notifications[0]["at"])

    def test_wait_notification_gives_none_when_the_bridge_exits_or_the_time_is_over(self):
        br, _ = self.bridge(json.dumps({"id": 1, "result": "one"}).encode() + b"\n")
        self.assertIsNone(br.wait_notification("notifications/tools/list_changed", 5))
        self.assertEqual(br.notes, ['{"id": 1, "result": "one"}'])
        with mock.patch.object(lab, "left", return_value=0):
            self.assertIsNone(br.wait_notification("notifications/x", 5))
        out = BlockingOut()
        self.addCleanup(out.release.set)
        blocked, _ = self.bridge(stdout=out)
        with mock.patch.object(lab, "left", return_value=0.2):
            self.assertIsNone(blocked.wait_notification("notifications/x", 5))

    def test_full_text_joins_the_text_blocks_of_a_response(self):
        resp = {"result": {"content": [{"type": "text", "text": "a"}, {"type": "image", "data": "x"}, "junk",
                                       {"type": "text", "text": "b"}, {"type": "text"}]}}
        self.assertEqual(lab.full_text(resp), "a\nb\n")
        self.assertEqual((lab.full_text(None), lab.full_text({}), lab.full_text({"result": "str"})), ("", "", ""))
        self.assertEqual(len(lab.full_text(ok_reply("x" * 5_000_100))), 5_000_000)

    def test_summarize_call_maps_every_kind_of_response(self):
        self.assertEqual(lab.summarize_call({"_timeout": True}), {"ok": False, "error": "timed out"})
        self.assertEqual(lab.summarize_call({"_bridge_exit": 3}), {"ok": False, "error": "the bridge exited (code 3)"})
        rpc = lab.summarize_call({"error": {"code": -32601, "message": "nope"}})
        self.assertEqual(rpc, {"ok": False, "rpc_error": '{"code": -32601, "message": "nope"}'})
        self.assertEqual(lab.summarize_call(ok_reply("plain text")),
                         {"isError": False, "text": "plain text", "ok": True})
        self.assertEqual(lab.summarize_call(ok_reply('{"fine": true}'))["ok"], True)
        self.assertEqual(lab.summarize_call({"result": {"content": []}}), {"isError": False, "text": "", "ok": True})
        self.assertEqual(lab.summarize_call({})["ok"], True)

    def test_summarize_call_fails_on_iserror_or_an_error_field_in_the_json_text(self):
        flagged = lab.summarize_call({"result": {"isError": True, "content": [{"type": "text", "text": "boom"}]}})
        self.assertEqual((flagged["ok"], flagged["isError"], "tool_error" in flagged), (False, True, False))
        tool = lab.summarize_call(ok_reply('{"error": "folder not found"}'))
        self.assertEqual((tool["ok"], tool["tool_error"], tool["isError"]), (False, "folder not found", False))
        self.assertEqual(lab.summarize_call(ok_reply('["error"]'))["ok"], True)
        self.assertEqual(lab.summarize_call(ok_reply('{"error": ""}'))["ok"], True)

    def test_summarize_call_clips_long_text(self):
        out = lab.summarize_call(ok_reply("y" * 9000))
        self.assertEqual(len(out["text"]), 8000 + len("...[truncated: 9000 characters in total]"))


# ------------------------------------------------------------------------------------------------ stages
class SourceTests(LabCase):
    def stdin_of(self, data):
        """A stand-in for sys.stdin whose buffer is a real file (tar is given its descriptor)."""
        f = open(self.put(self.tmp + "/stdin.bin", data), "rb")
        self.addCleanup(f.close)
        return types.SimpleNamespace(buffer=f, close=mock.Mock())

    def tar_stdin(self, entries):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w") as t:
            for name, data in entries.items():
                info = tarfile.TarInfo(name)
                if isinstance(data, tuple):
                    info.type, info.linkname = tarfile.SYMTYPE, data[0]
                    t.addfile(info)
                else:
                    info.size = len(data)
                    t.addfile(info, io.BytesIO(data))
        return self.stdin_of(out.getvalue())

    def tree(self, **replace):
        entries = {"extension/manifest.json": b"{}", "extension/mcp_server/api.js": b"api", "mcp-bridge.cjs": b"bridge",
                   "package.json": b"{}"}
        entries.update(replace)
        return {k: v for k, v in entries.items() if v is not None}

    def test_extract_source_unpacks_the_tree_and_records_hashes(self):
        stdin = self.tar_stdin(self.tree())
        with mock.patch.object(sys, "stdin", stdin):
            lab.extract_source()
        stdin.close.assert_called_once_with()
        with open(lab.SRC + "/mcp-bridge.cjs", "rb") as f:
            self.assertEqual(f.read(), b"bridge")
        info = lab.R["phases"]["source"]
        self.assertEqual(info["files"], 4)
        self.assertEqual(info["sha256"], {"extension/manifest.json": hashlib.sha256(b"{}").hexdigest(),
                                          "extension/mcp_server/api.js": hashlib.sha256(b"api").hexdigest(),
                                          "mcp-bridge.cjs": hashlib.sha256(b"bridge").hexdigest()})

    def test_extract_source_survives_a_standard_input_that_cannot_be_closed(self):
        stdin = self.tar_stdin(self.tree())
        stdin.close.side_effect = OSError("bad fd")
        with mock.patch.object(sys, "stdin", stdin):
            lab.extract_source()
        stdin.close.assert_called_once_with()
        self.assertIn("source", lab.R["phases"])

    def test_extract_source_reports_a_tar_that_cannot_be_read(self):
        with mock.patch.object(sys, "stdin", self.stdin_of(b"x" * 1024)):
            with self.assertRaisesRegex(RuntimeError, "^tar extraction failed: .*tar archive"):
                lab.extract_source()

    def test_extract_source_requires_the_three_files_as_regular_files(self):
        for missing in ("extension/manifest.json", "mcp-bridge.cjs", "package.json"):
            with self.subTest(missing=missing):
                shutil.rmtree(lab.SRC, ignore_errors=True)
                stdin = self.tar_stdin(self.tree(**{missing: None}))
                with mock.patch.object(sys, "stdin", stdin), self.assertRaisesRegex(
                        RuntimeError, "^expected file missing from the received tree: " + re.escape(missing) + "$"):
                    lab.extract_source()
        shutil.rmtree(lab.SRC, ignore_errors=True)
        stdin = self.tar_stdin(self.tree(**{"mcp-bridge.cjs": ("package.json",)}))
        with mock.patch.object(sys, "stdin", stdin), self.assertRaisesRegex(RuntimeError, "mcp-bridge.cjs$"):
            lab.extract_source()


class ExtensionTests(LabCase):
    def manifest(self, gid=GECKO_ID, key="browser_specific_settings", **extra):
        return dict({"name": "x", "version": "1.2.3", key: {"gecko": {"id": gid, "strict_min_version": "128.0"}}},
                    **extra)

    def zip_xpi(self, rel, files, compression=zipfile.ZIP_DEFLATED):
        path = self.put(lab.SRC + "/" + rel)
        with zipfile.ZipFile(path, "w", compression) as z:
            for name, data in files.items():
                z.writestr(name, data)
        return path

    def test_gecko_selects_the_connection_file_of_the_installed_id(self):
        for gid, conn in (("thunderbird-mcp@tkasperczyk.dev", "/tmp/thunderbird-mcp/connection.json"),
                          (GECKO_ID, "/tmp/commonpost-mcp/connection.json")):
            gecko, got = lab._gecko(self.manifest(gid, key="applications"))
            self.assertEqual((got, lab.CONN, gecko["strict_min_version"]), (gid, conn, "128.0"))
        self.assertEqual(lab.R["errors"], [])

    def test_gecko_keeps_an_unexpected_but_valid_id_with_an_error(self):
        _, gid = lab._gecko(self.manifest("other@example.org"))
        self.assertEqual(gid, "other@example.org")
        self.assertEqual(lab.CONN, self.tmp + "/conn.json")
        self.assertEqual(len(lab.R["errors"]), 1)
        self.assertTrue(lab.R["errors"][0].startswith("unexpected gecko id: other@example.org (expected one of "))

    def test_gecko_refuses_an_invalid_id(self):
        for mf in ({}, {"browser_specific_settings": {}}, self.manifest(""), self.manifest("ab"),
                   self.manifest("a..b@x.y"), self.manifest("has space@x.y"), self.manifest("x" * 101),
                   self.manifest("a/b@x.y"), self.manifest("abc@x.y\n")):
            with self.subTest(mf=mf), self.assertRaisesRegex(RuntimeError, "invalid gecko id in the manifest"):
                lab._gecko(mf)

    def test_prebuilt_xpi_is_copied_as_is_under_the_extension_id(self):
        src = self.zip_xpi("dist/ext.xpi", {"manifest.json": json.dumps(self.manifest()), "bg.js": "x"})
        self.assertEqual(lab.install_prebuilt_xpi("dist/ext.xpi"), (GECKO_ID, "1.2.3"))
        dst = "%s/extensions/%s.xpi" % (lab.PROF, GECKO_ID)
        with open(src, "rb") as a, open(dst, "rb") as b:
            self.assertEqual(a.read(), b.read())
        info = lab.R["phases"]["xpi"]
        self.assertEqual((info["source"], info["path"], info["id"], info["version"], info["files"]),
                         ("prebuilt", "dist/ext.xpi", GECKO_ID, "1.2.3", 2))
        self.assertEqual((info["sha256"], info["strict_min_version"]), (lab.sha256(dst), "128.0"))

    def test_prebuilt_xpi_must_be_a_regular_file_inside_the_received_tree(self):
        real = self.zip_xpi("real.xpi", {"manifest.json": json.dumps(self.manifest())})
        os.symlink(real, lab.SRC + "/link.xpi")
        self.put(self.tmp + "/lab/outside.xpi", b"x")
        for rel in ("missing.xpi", "link.xpi", "../outside.xpi", "."):
            with self.subTest(rel=rel), self.assertRaisesRegex(
                    RuntimeError, "xpi: missing, a link, or outside the received tree"):
                lab.install_prebuilt_xpi(rel)

    def test_prebuilt_xpi_larger_than_60_mb_is_refused(self):
        self.zip_xpi("big.xpi", {"manifest.json": "{}"})
        with mock.patch.object(lab.os.path, "getsize", return_value=60_000_001):
            with self.assertRaisesRegex(RuntimeError, r"xpi too large \(> 60 MB\)"):
                lab.install_prebuilt_xpi("big.xpi")

    def test_prebuilt_xpi_with_a_corrupt_entry_is_refused(self):
        path = self.zip_xpi("bad.xpi", {"manifest.json": json.dumps(self.manifest())}, zipfile.ZIP_STORED)
        with open(path, "rb") as f:
            data = f.read()
        with open(path, "wb") as f:
            f.write(data.replace(b'"1.2.3"', b'"1.2.4"', 1))
        with self.assertRaisesRegex(RuntimeError, "xpi: corrupt entry 'manifest.json'"):
            lab.install_prebuilt_xpi("bad.xpi")

    def test_build_xpi_zips_extension_without_links(self):
        self.put(lab.SRC + "/extension/manifest.json", json.dumps(self.manifest()))
        self.put(lab.SRC + "/extension/mcp_server/api.js", b"api")
        self.put(lab.SRC + "/elsewhere/secret.txt", b"secret")
        os.symlink(lab.SRC + "/elsewhere/secret.txt", lab.SRC + "/extension/link.js")
        os.symlink(lab.SRC + "/elsewhere", lab.SRC + "/extension/linkdir")
        self.assertEqual(lab.build_xpi({}), (GECKO_ID, "1.2.3"))
        with zipfile.ZipFile("%s/extensions/%s.xpi" % (lab.PROF, GECKO_ID)) as z:
            self.assertEqual(z.namelist(), ["manifest.json", "mcp_server/api.js"])
        info = lab.R["phases"]["xpi"]
        self.assertEqual((info["source"], info["files"], info["skipped"]), ("extension/", 2, ["link.js"]))

    def test_build_xpi_requires_a_real_extension_directory(self):
        os.makedirs(lab.SRC)
        with self.assertRaisesRegex(RuntimeError, r"extension/ missing or a link"):
            lab.build_xpi({})
        self.put(self.tmp + "/real/manifest.json", "{}")
        os.symlink(self.tmp + "/real", lab.SRC + "/extension")
        with self.assertRaisesRegex(RuntimeError, r"extension/ missing or a link"):
            lab.build_xpi({})

    def test_build_xpi_refuses_an_extension_larger_than_60_mb(self):
        self.put(lab.SRC + "/extension/manifest.json", json.dumps(self.manifest()))
        self.put(lab.SRC + "/extension/big.bin", b"x")
        with mock.patch.object(lab.os.path, "getsize", return_value=30_000_001):
            with self.assertRaisesRegex(RuntimeError, r"extension too large \(> 60 MB\)"):
                lab.build_xpi({})

    def test_build_xpi_delegates_to_a_prebuilt_xpi_when_the_scenario_names_one(self):
        self.zip_xpi("x.xpi", {"manifest.json": json.dumps(self.manifest())})
        self.assertEqual(lab.build_xpi({"xpi": "x.xpi"}), (GECKO_ID, "1.2.3"))
        self.assertEqual(lab.R["phases"]["xpi"]["source"], "prebuilt")


class ServiceTests(LabCase):
    def fake_start(self, name, argv, **kw):
        self.started.append((name, argv, kw))
        return FakeProc()

    def setUp(self):
        super().setUp()
        self.started = []
        patcher = mock.patch.object(lab, "start", side_effect=self.fake_start)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_start_mailpit_fails_with_the_end_of_its_log_when_the_api_never_answers(self):
        os.makedirs(lab.LAB)
        self.put(lab.LOGS + "/mailpit.log", "listening\nfatal: port busy\n")
        with mock.patch.object(lab, "wait_for", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "^mailpit does not answer:\nlistening\nfatal: port busy$"):
                lab.start_mailpit()
        with open(lab.LAB + "/pop3-auth") as f:
            self.assertEqual(f.read(), "lab:lab\n")
        name, argv, _ = self.started[0]
        self.assertEqual(name, "mailpit")
        self.assertEqual(argv[argv.index("--pop3-auth-file") + 1], lab.LAB + "/pop3-auth")
        self.assertIn("--smtp-auth-accept-any", argv)

    def run_mailpit(self, api, pop):
        os.makedirs(lab.LAB)
        urls = []

        def wait(pred, timeout, step=0.25):
            return pred()

        def fake_http(method, url, timeout=10):
            urls.append((method, url))
            return 200, b""

        def fake_json(url, timeout=10):
            urls.append(("GET", url))
            return api
        smtp = mock.MagicMock()
        with mock.patch.object(lab, "wait_for", side_effect=wait), mock.patch.object(lab, "http", fake_http), \
                mock.patch.object(lab, "http_json", fake_json), mock.patch.object(lab.smtplib, "SMTP", smtp), \
                mock.patch.object(lab.poplib, "POP3", pop):
            try:
                lab.start_mailpit()
                error = None
            except RuntimeError as e:
                error = str(e)
        return urls, smtp, error

    def test_start_mailpit_checks_smtp_to_api_and_pop3(self):
        pop = mock.MagicMock()
        pop.return_value.stat.return_value = (3, 100)
        urls, smtp, error = self.run_mailpit({"messages_count": 1, "Version": "v9"}, pop)
        self.assertIsNone(error)
        marker = "lab-selfcheck-%d" % os.getpid()
        sent = smtp.return_value.__enter__.return_value.sendmail.call_args[0]
        self.assertIn("Subject: " + marker + "\r\n", sent[2].decode())
        self.assertIn(("GET", lab.API + "/search?query=" + marker), urls)
        self.assertIn(("DELETE", lab.API + "/messages"), urls)
        pop.return_value.user.assert_called_once_with("lab")
        pop.return_value.pass_.assert_called_once_with("lab")
        info = lab.R["phases"]["mailpit"]
        self.assertEqual((info["smtp_to_api"], info["pop3_messages"], info["version"]), (True, 3, "v9"))

    def test_start_mailpit_fails_when_the_message_never_reaches_the_api(self):
        _, _, error = self.run_mailpit({"messages_count": 0}, mock.MagicMock(side_effect=OSError("refused")))
        self.assertEqual(error, "mailpit self-check failed")
        info = lab.R["phases"]["mailpit"]
        self.assertEqual((info["smtp_to_api"], info["pop3_messages"]), (False, "failed: refused"))

    def test_start_xvfb_records_the_display_or_the_end_of_its_log(self):
        self.put(lab.LOGS + "/xvfb.log", "Xvfb: no screen\n")
        with mock.patch.object(lab.os, "makedirs") as makedirs, mock.patch.object(lab.os, "chmod") as chmod:
            with mock.patch.object(lab, "wait_for", return_value=True):
                lab.start_xvfb()
            self.assertEqual(lab.R["phases"]["xvfb"]["display"], ":99")
            with mock.patch.object(lab, "wait_for", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "^Xvfb did not start:\nXvfb: no screen$"):
                    lab.start_xvfb()
        makedirs.assert_called_with("/tmp/.X11-unix", exist_ok=True)
        chmod.assert_called_with("/tmp/.X11-unix", 0o1777)
        self.assertEqual(self.started[0][1][:2], ["Xvfb", ":99"])

    def start_tb(self, sc, conn=None, code=None, wait=True):
        proc = FakeProc(code=code)

        def fake_start(name, argv, env=None, **kw):
            lab.PROCS[name] = proc
            self.started.append((name, argv, env))
            return proc
        if conn is not None:
            self.put(lab.CONN, conn)
        waits = []

        def fake_wait(pred, timeout, step=0.25):
            waits.append((timeout, step))
            return pred() if wait else None
        self.put(lab.LOGS + "/thunderbird.log", "tb line 1\ntb line 2\n")
        with mock.patch.object(lab, "start", side_effect=fake_start), mock.patch.object(lab, "wait_for", fake_wait), \
                mock.patch.object(lab.os, "makedirs"), mock.patch.object(lab, "TB_DIR", self.tmp + "/tb"):
            try:
                lab.start_tb(sc)
            finally:
                self.waits = waits
        return self.started[-1]

    def test_start_tb_runs_thunderbird_on_the_profile_with_its_sandbox_by_default(self):
        self.put(self.tmp + "/tb/application.ini", "[App]\nVersion=128.1.0\nBuildID=2026\nName=x\n")
        name, argv, env = self.start_tb({"startup_timeout": 90}, conn='{"port": 4321}')
        self.assertEqual((name, argv), ("thunderbird", [self.tmp + "/tb/thunderbird", "--profile", lab.PROF,
                                                         "--no-remote"]))
        self.assertEqual((env["DISPLAY"], env["MOZ_NO_REMOTE"], env["XDG_RUNTIME_DIR"]), (":99", "1", "/tmp/xdg"))
        self.assertFalse([k for k in env if k.startswith("MOZ_DISABLE_") and k.endswith("SANDBOX")])
        self.assertEqual(self.waits, [(90, 0.5)])
        info = lab.R["phases"]["thunderbird"]
        self.assertEqual((info["port"], info["content_sandbox"], info["version"]),
                         (4321, True, {"Version": "128.1.0", "BuildID": "2026"}))

    def test_start_tb_can_turn_its_sandbox_off(self):
        _, _, env = self.start_tb({"tb_sandbox": False}, conn='{"port": 1}')
        self.assertEqual(sorted(k for k, v in env.items() if k.startswith("MOZ_DISABLE_") and v == "1"), [
            "MOZ_DISABLE_AUTO_SAFE_MODE", "MOZ_DISABLE_CONTENT_SANDBOX", "MOZ_DISABLE_GMP_SANDBOX",
            "MOZ_DISABLE_GPU_SANDBOX", "MOZ_DISABLE_RDD_SANDBOX", "MOZ_DISABLE_SOCKET_PROCESS_SANDBOX",
            "MOZ_DISABLE_UTILITY_SANDBOX"])
        self.assertIs(lab.R["phases"]["thunderbird"]["content_sandbox"], False)
        self.assertEqual(self.waits, [(150, 0.5)])

    def test_start_tb_tolerates_a_connection_file_without_a_port(self):
        self.start_tb({}, conn="not json")
        self.assertIsNone(lab.R["phases"]["thunderbird"]["port"])
        self.assertEqual(lab.R["phases"]["thunderbird"]["version"], {})

    def test_start_tb_reports_a_thunderbird_that_exited_before_the_connection_file(self):
        with self.assertRaisesRegex(
                RuntimeError, r"\(Thunderbird exited with code 3\); end of its log:\ntb line 1\ntb line 2$"):
            self.start_tb({}, code=3)

    def test_start_tb_reports_a_thunderbird_that_is_still_running(self):
        with self.assertRaisesRegex(RuntimeError, r"\(Thunderbird still running\); end of its log:\ntb line 1"):
            self.start_tb({}, wait=False)

    def test_node_version_is_the_trimmed_output_or_none(self):
        run = mock.Mock(return_value=types.SimpleNamespace(stdout=b"v22.1.0\n"))
        with mock.patch.object(lab.subprocess, "run", run):
            self.assertEqual(lab.node_version(), "v22.1.0")
        self.assertEqual(run.call_args[0][0], ["node", "--version"])
        with mock.patch.object(lab.subprocess, "run", side_effect=FileNotFoundError("node")):
            self.assertIsNone(lab.node_version())


class ProfileTests(LabCase):
    def test_seed_mbox_appends_messages_in_thunderbird_format(self):
        sc = {"seed": [{"folder": "Local Folders/Inbox", "messages": [
            "From: a@x\r\nSubject: s\r\n\r\nbody\r\nFrom the start\r\n", "Subject: no newline\n\nbody"]}]}
        lab.seed_mbox(sc)
        path = lab.PROF + "/Mail/Local Folders/Inbox"
        with open(path, "rb") as f:
            data = f.read()
        self.assertEqual(data, (b"From - Thu Jan 01 00:00:00 2026\r\nFrom: a@x\r\nSubject: s\r\n\r\nbody\r\n"
                                b">From the start\r\n\r\n"
                                b"From - Thu Jan 01 00:00:00 2026\r\nSubject: no newline\r\n\r\nbody\r\n\r\n"))
        self.assertEqual([lab.analyse_message(m)["subject"] for m in lab.split_mbox(data)], ["s", "no newline"])
        self.assertEqual(lab.R["phases"]["seed"]["folders"], {"Local Folders/Inbox": {"messages": 2,
                                                                                      "bytes": len(data)}})
        lab.seed_mbox(sc)
        self.assertEqual(os.path.getsize(path), 2 * len(data))

    def test_seed_mbox_without_seeds_records_nothing_and_refuses_paths_outside_the_profile(self):
        lab.seed_mbox({})
        self.assertNotIn("seed", lab.R["phases"])
        with self.assertRaisesRegex(RuntimeError, r"seed: path outside the profile: '\.\./\.\./x/y'"):
            lab.seed_mbox({"seed": [{"folder": "../../x/y", "messages": ["m"]}]})

    def test_seed_files_write_the_content_untranslated_under_mail(self):
        content = "version=\"9\"\r\nname=\"r\"\n"
        lab.seed_files({"seed_files": [{"path": "Mail/127.0.0.1/msgFilterRules.dat", "content": content}]})
        path = lab.PROF + "/Mail/127.0.0.1/msgFilterRules.dat"
        with open(path, "rb") as f:
            self.assertEqual(f.read(), content.encode())
        self.assertEqual(lab.R["phases"]["seed_files"]["files"], {
            "Mail/127.0.0.1/msgFilterRules.dat": {"bytes": len(content), "sha256": lab.sha256(path)}})

    def test_seed_files_without_files_records_nothing_and_refuses_paths_outside_mail(self):
        lab.seed_files({})
        self.assertNotIn("seed_files", lab.R["phases"])
        with self.assertRaisesRegex(RuntimeError, "seed_files: path outside the profile"):
            lab.seed_files({"seed_files": [{"path": "Mail/../../escape.dat", "content": "x"}]})

    def test_make_profile_writes_user_js_with_the_scenario_preferences_and_the_seeds(self):
        base = self.put(self.tmp + "/base-user.js", 'user_pref("base", 1);\n')
        sc = {"prefs": {"a.b": True, "n": 5, "s": 'q"'}, "seed": [{"folder": "Local Folders/Inbox", "messages": ["m"]}],
              "seed_files": [{"path": "Mail/Local Folders/msgFilterRules.dat", "content": "x"}]}
        with mock.patch.object(lab, "USERJS_BASE", base):
            lab.make_profile(sc)
        with open(lab.PROF + "/user.js") as f:
            self.assertEqual(f.read(), 'user_pref("base", 1);\n\n// --- Scenario preferences\n'
                                       'user_pref("a.b", true);\nuser_pref("n", 5);\nuser_pref("s", "q\\"");\n')
        self.assertEqual(lab.R["phases"]["profile"]["scenario_prefs"], 3)
        self.assertTrue(os.path.isfile(lab.PROF + "/Mail/Local Folders/Inbox"))
        self.assertTrue(os.path.isfile(lab.PROF + "/Mail/Local Folders/msgFilterRules.dat"))

    def test_tb_version_reads_the_known_keys_of_application_ini(self):
        self.put(self.tmp + "/tb/application.ini", "[App]\nVendor=Mozilla\nVersion=128.1.0\nSourceStamp=abc\n"
                                                     "SourceRepository=https://hg.example/x=y\n")
        with mock.patch.object(lab, "TB_DIR", self.tmp + "/tb"):
            self.assertEqual(lab.tb_version(), {"Version": "128.1.0", "SourceStamp": "abc",
                                                "SourceRepository": "https://hg.example/x=y"})
        with mock.patch.object(lab, "TB_DIR", self.tmp + "/none"):
            self.assertEqual(lab.tb_version(), {})

    def test_read_coverage_accepts_a_hex_bitmap(self):
        self.put(lab.PROF + "/cp-coverage.txt", b"00ff0a9c")
        lab.read_coverage()
        self.assertEqual(lab.R["coverage_hex"], "00ff0a9c")
        self.assertNotIn("coverage_error", lab.R)

    def test_read_coverage_accepts_an_empty_bitmap_and_the_maximum_length(self):
        self.put(lab.PROF + "/cp-coverage.txt", b"")
        lab.read_coverage()
        self.assertEqual(lab.R["coverage_hex"], "")
        self.put(lab.PROF + "/cp-coverage.txt", b"a" * lab.MAX_COVERAGE)
        lab.read_coverage()
        self.assertEqual(len(lab.R["coverage_hex"]), lab.MAX_COVERAGE)

    def test_read_coverage_refuses_anything_that_is_not_a_short_lowercase_hex_string(self):
        for data in (b"xyz", b"FF", b"0f 0f", b"0" * (lab.MAX_COVERAGE + 1), b"\xff\xfe"):
            with self.subTest(data=data[:10]):
                lab.R.pop("coverage_hex", None)
                self.put(lab.PROF + "/cp-coverage.txt", data)
                lab.read_coverage()
                self.assertNotIn("coverage_hex", lab.R)
                self.assertEqual(lab.R["coverage_error"],
                                 "cp-coverage.txt is not a hex bitmap of at most 65536 characters")

    def test_read_coverage_ignores_a_missing_file_a_link_and_a_directory(self):
        lab.read_coverage()
        self.put(self.tmp + "/real.txt", b"00")
        os.makedirs(lab.PROF)
        os.symlink(self.tmp + "/real.txt", lab.PROF + "/cp-coverage.txt")
        lab.read_coverage()
        os.unlink(lab.PROF + "/cp-coverage.txt")
        os.mkdir(lab.PROF + "/cp-coverage.txt")
        lab.read_coverage()
        self.assertEqual({k for k in lab.R if k.startswith("coverage")}, set())


class PrefWatchTests(LabCase):
    PREFS = ('user_pref("mail.a", 1);\nuser_pref("other.b", 2);\nuser_pref("mail.c", "x");\nnot a pref line\n')

    def wait_until(self, pred):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            if pred():
                return True
            time.sleep(0.02)
        return False

    def test_snapshot_keeps_the_lines_of_the_watched_prefixes(self):
        self.put(lab.PROF + "/prefs.js", self.PREFS)
        pw = lab.PrefWatch(["mail."])
        self.addCleanup(pw.stop)
        self.assertEqual(pw.snapshot(), ['user_pref("mail.a", 1);', 'user_pref("mail.c", "x");'])

    def test_snapshot_is_none_without_prefs_js(self):
        pw = lab.PrefWatch(["mail."])
        self.addCleanup(pw.stop)
        self.assertIsNone(pw.snapshot())

    def test_the_timeline_records_each_change_with_the_current_step(self):
        pw = lab.PrefWatch(["mail."])
        pw.stage = "step-1"
        self.put(lab.PROF + "/prefs.js", self.PREFS)
        self.assertTrue(self.wait_until(lambda: len(pw.timeline) == 1))
        self.assertEqual((pw.timeline[0]["step"], pw.timeline[0]["prefs_js"]),
                         ("step-1", ['user_pref("mail.a", 1);', 'user_pref("mail.c", "x");']))
        pw.stage = "step-2"
        self.put(lab.PROF + "/prefs.js", 'user_pref("mail.a", 2);\n')
        self.assertTrue(self.wait_until(lambda: len(pw.timeline) == 2))
        self.assertEqual((pw.timeline[1]["step"], pw.timeline[1]["prefs_js"]), ("step-2", ['user_pref("mail.a", 2);']))
        pw.stage = "step-3"
        os.unlink(lab.PROF + "/prefs.js")
        self.assertTrue(self.wait_until(lambda: len(pw.timeline) == 3))
        self.assertEqual((pw.timeline[2]["step"], pw.timeline[2]["prefs_js"]), ("step-3", "missing"))
        pw.stop()
        self.assertIs(lab.R["prefs_timeline"], pw.timeline)
        self.assertFalse(pw.th.is_alive())
        self.assertEqual(len(pw.timeline), 3)

    def test_stop_takes_a_last_snapshot(self):
        self.put(lab.PROF + "/prefs.js", self.PREFS)
        pw = lab.PrefWatch(["mail."])
        self.assertTrue(self.wait_until(lambda: len(pw.timeline) == 1))
        self.put(lab.PROF + "/prefs.js", 'user_pref("mail.z", 9);\n')
        pw.stage = "end"
        pw.stop()
        self.assertEqual(pw.timeline[-1]["prefs_js"], ['user_pref("mail.z", 9);'])
        self.assertEqual(lab.R["prefs_timeline"], pw.timeline)

    def test_stop_without_any_prefs_js_records_an_empty_timeline(self):
        pw = lab.PrefWatch(["mail."])
        pw.stop()
        self.assertEqual(lab.R["prefs_timeline"], [])


# ------------------------------------------------------------------------------------------------ MCP steps
class McpTests(LabCase):
    TOOLS = [{"name": "listFolders", "inputSchema": {"properties": {"b": {}, "a": {}}}}, {"name": "sendMail"},
             "junk"]

    def rpc(self, init=None):
        return {"initialize": init or {"result": {"serverInfo": {"name": "srv"}, "protocolVersion": "2025-06-18"}},
                "tools/list": {"result": {"tools": self.TOOLS}}}

    def run_mcp(self, sc, br=None, groups=None):
        br = br or FakeBridge(rpc=self.rpc())
        with mock.patch.object(lab, "node_version", return_value="v22"), \
                mock.patch.object(lab, "run_group", side_effect=lambda grp, by_name, b: (
                    groups.append([s["id"] for s in grp]) if groups is not None else None)):
            lab.run_mcp(sc, br)
        return br

    def test_run_mcp_initializes_lists_the_tools_and_records_what_the_scenario_watches(self):
        br = self.run_mcp({"schema_watch": ["listFolders", "absent"], "tools_dump": ["listFolders", "absent"]})
        self.assertEqual([r[0] for r in br.requests], ["initialize", "tools/list"])
        self.assertEqual(br.requests[0][1]["clientInfo"]["name"], "thunderbird-mcp-lab")
        self.assertEqual(br.sent, [{"jsonrpc": "2.0", "method": "notifications/initialized"}])
        info = lab.R["phases"]["mcp"]
        self.assertEqual((info["server"], info["protocol"], info["tools"], info["node"]),
                         ({"name": "srv"}, "2025-06-18", 3, "v22"))
        self.assertEqual(info["schemas"], {"listFolders": ["a", "b"], "absent": []})
        self.assertEqual(lab.R["tools"], ["listFolders", "sendMail"])
        self.assertEqual(sorted(lab.TOOLS), ["listFolders", "sendMail"])
        self.assertEqual(lab.R["tools_detail"], {"listFolders": self.TOOLS[0], "absent": None})
        expected = hashlib.sha256(json.dumps(self.TOOLS[0], sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        self.assertEqual(lab.R["tools_detail_sha256"], {"listFolders": expected, "absent": None})
        self.assertEqual(lab.R["steps"], [])

    def test_run_mcp_does_not_initialize_again_after_a_cold_start(self):
        br = FakeBridge(rpc=self.rpc())
        with mock.patch.object(lab, "node_version", return_value="v22"), mock.patch.object(lab, "run_group"):
            lab.run_mcp({}, br, {"result": {"serverInfo": {"name": "cold"}, "protocolVersion": "2025-06-18"}})
        self.assertEqual([r[0] for r in br.requests], ["tools/list"])
        self.assertEqual(br.sent, [])
        self.assertEqual(lab.R["phases"]["mcp"]["server"], {"name": "cold"})

    def test_run_mcp_fails_when_initialize_does_not_return_a_result(self):
        br = FakeBridge(rpc=self.rpc({"error": {"code": -1, "message": "no"}}))
        with self.assertRaisesRegex(RuntimeError, r'^initialize failed: \{"error": \{"code": -1'):
            self.run_mcp({}, br)

    def test_run_mcp_sends_consecutive_steps_of_a_batch_together(self):
        steps = [{"id": "a", "tool": "t"}, {"id": "b", "tool": "t", "batch": "x"},
                 {"id": "c", "tool": "t", "batch": "x"}, {"id": "d", "tool": "t", "batch": "y"},
                 {"id": "e", "tool": "t", "batch": "x"}, {"id": "f", "tool": "t"}]
        groups = []
        self.run_mcp({"steps": steps}, groups=groups)
        self.assertEqual(groups, [["a"], ["b", "c"], ["d"], ["e"], ["f"]])

    def test_run_mcp_marks_the_end_of_the_steps_and_keeps_the_bridge_notes(self):
        lab.PW = types.SimpleNamespace(stage="x")
        br = FakeBridge(rpc=self.rpc())
        br.notes = ["n%d" % i for i in range(25)]
        self.run_mcp({}, br)
        self.assertEqual(lab.PW.stage, "after_steps")
        self.assertEqual(lab.R["bridge_other_messages"], br.notes[:20])


class ColdBridge:
    """The part of Bridge that run_cold_start uses; records the order of what happens in `events`."""

    def __init__(self, events, answer, at=3.5, notified=None):
        self.events, self.answer, self.at, self.notified = events, answer, at, notified
        self.sent = []

    def request(self, method, params, timeout):
        self.events.append(method)
        return {"result": {"capabilities": {"tools": {"listChanged": True}}, "serverInfo": {"name": "s"}}}

    def send(self, obj):
        self.sent.append(obj)

    def send_request(self, method, params):
        self.events.append("send " + method)
        return 7

    def wait_responses(self, rids, timeout):
        self.events.append("answer")
        return {7: (self.answer, self.at)}

    def wait_notification(self, method, timeout):
        self.events.append("wait " + method)
        return self.notified


class ColdStartTests(LabCase):
    def run_cold(self, bridge_first, answer, **kw):
        events = []
        br = ColdBridge(events, answer, **kw)
        with mock.patch.object(lab, "start_tb", side_effect=lambda sc: events.append("start_tb")), \
                mock.patch.object(lab, "T0", 100.0), \
                mock.patch.object(lab, "time", types.SimpleNamespace(
                    monotonic=lambda: 101.0, sleep=lambda s: events.append("sleep %s" % s))):
            init = lab.run_cold_start({"bridge_first": bridge_first}, br)
        return init, events, br

    def test_with_true_thunderbird_starts_once_tools_list_is_answered(self):
        init, events, br = self.run_cold(True, {"result": {"tools": []}}, notified=4.0)
        self.assertEqual(events, ["initialize", "send tools/list", "answer", "start_tb",
                                  "wait notifications/tools/list_changed"])
        self.assertEqual(br.sent, [{"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual(init["result"]["serverInfo"], {"name": "s"})
        self.assertEqual(lab.R["cold_start"], {"mode": True, "capabilities": {"tools": {"listChanged": True}},
                                               "tools": 0, "seconds": 2.5, "list_changed_after": 3.0})
        self.assertEqual(lab.R["phases"]["cold_start"]["tools"], 0)

    def test_with_seconds_thunderbird_starts_while_tools_list_waits(self):
        _, events, _ = self.run_cold(10, {"result": {"tools": [{"name": "a"}, {"name": "b"}]}})
        self.assertEqual(events, ["initialize", "send tools/list", "sleep 10.0", "start_tb", "answer",
                                  "wait notifications/tools/list_changed"])
        self.assertEqual((lab.R["cold_start"]["tools"], lab.R["cold_start"]["list_changed_after"]), (2, None))

    def test_an_answer_that_is_not_a_tool_list_is_recorded_as_an_error(self):
        self.run_cold(True, {"error": {"code": -32603, "message": "Bridge error: Connection discovery failed"}})
        cs = lab.R["cold_start"]
        self.assertIsNone(cs["tools"])
        self.assertIn("Connection discovery failed", cs["error"])
        self.run_cold(True, {"result": "odd"})
        self.assertIsNone(lab.R["cold_start"]["tools"])
        self.run_cold(True, {"_timeout": True}, at=None)
        self.assertEqual((lab.R["cold_start"]["tools"], lab.R["cold_start"]["seconds"]), (None, None))


class GroupTests(LabCase):
    def setUp(self):
        super().setUp()
        lab.R["steps"] = []          # created by run_mcp before the first group

    def group(self, steps, bridge, tools=("t",)):
        lab.run_group(steps, {n: {} for n in tools}, bridge)
        return lab.R["steps"]

    def test_a_successful_call_records_the_response_the_timing_and_the_full_text(self):
        br = FakeBridge({"t": ok_reply("hello")})
        [e] = self.group([{"id": "s1", "tool": "t", "arguments": {"k": 1}}], br)
        self.assertEqual((e["id"], e["tool"], e["arguments"]), ("s1", "t", {"k": 1}))
        self.assertEqual((e["ok"], e["text"], e["answered_at"]), (True, "hello", 1.5))
        self.assertIsInstance(e["sent_at"], float)
        self.assertEqual(lab.FULLTEXT, {"s1": "hello"})
        self.assertEqual(br.calls, [("tools/call", {"name": "t", "arguments": {"k": 1}})])
        self.assertEqual(br.waits, [([1], 60)])

    def test_a_tool_missing_from_tools_list_is_not_called(self):
        br = FakeBridge({})
        [e] = self.group([{"id": "s1", "tool": "ghost"}], br)
        self.assertEqual((e["ok"], e["error"], e["not_in_tools_list"]), (False, "tool not in tools/list", True))
        self.assertEqual((br.calls, br.waits), ([], []))

    def test_force_call_calls_a_hidden_tool_to_record_the_refusal(self):
        br = FakeBridge({"ghost": ok_reply('{"error": "disabled"}')})
        [e] = self.group([{"id": "s1", "tool": "ghost", "force_call": True}], br)
        self.assertEqual((e["not_in_tools_list"], e["ok"], e["tool_error"]), (True, False, "disabled"))
        self.assertEqual(len(br.calls), 1)

    def test_a_batch_sends_every_request_before_waiting_with_the_longest_timeout(self):
        br = FakeBridge({"t": ok_reply("one"), "u": ok_reply("two")})
        steps = [{"id": "a", "tool": "t", "batch": "b1", "timeout": 5}, {"id": "b", "tool": "u", "batch": "b1",
                                                                     "timeout": 20}]
        entries = self.group(steps, br, tools=("t", "u"))
        self.assertEqual([(e["id"], e["batch"], e["text"]) for e in entries], [("a", "b1", "one"), ("b", "b1", "two")])
        self.assertEqual(br.waits, [([1, 2], 20)])

    def test_generated_arguments_are_sent_expanded_and_recorded_compactly(self):
        br = FakeBridge({"t": ok_reply("ok")})
        args = {"data": {"$lab_base64": {"bytes": 3}}}
        [e] = self.group([{"id": "s1", "tool": "t", "arguments": args}], br)
        self.assertEqual(br.calls[0][1]["arguments"], {"data": "YWFh"})
        self.assertEqual(e["arguments"], args)
        self.assertEqual(e["generated_arguments"][0]["bytes"], 3)

    def test_a_retry_repeats_a_failing_call_until_it_succeeds(self):
        br = FakeBridge({"t": [ok_reply('{"error": "busy"}'), ok_reply('{"error": "busy"}'), ok_reply("done")]})
        with mock.patch.object(lab.time, "sleep") as sleep:
            [e] = self.group([{"id": "s1", "tool": "t", "retry": {"attempts": 5, "delay": 3}}], br)
        self.assertEqual((e["ok"], e["attempts"], e["text"]), (True, 3, "done"))
        self.assertEqual(len(br.calls), 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [3, 3])

    def test_a_retry_gives_up_after_the_last_attempt(self):
        br = FakeBridge({"t": ok_reply('{"error": "busy"}')})
        with mock.patch.object(lab.time, "sleep") as sleep:
            [e] = self.group([{"id": "s1", "tool": "t", "retry": {"attempts": 2}}], br)
        self.assertEqual((e["ok"], e["attempts"], len(br.calls)), (False, 2, 2))
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2])

    def test_wait_after_sleeps_but_never_past_the_deadline(self):
        br = FakeBridge({"t": ok_reply("ok")})
        with mock.patch.object(lab.time, "sleep") as sleep:
            self.group([{"id": "s1", "tool": "t", "wait_after": 10}], br)
            self.group([{"id": "s2", "tool": "t", "wait_after": 10}], br)
            with mock.patch.object(lab, "DEADLINE", time.monotonic() + 35):
                self.group([{"id": "s3", "tool": "t", "wait_after": 10}], br)
        times = [c.args[0] for c in sleep.call_args_list]
        self.assertEqual(times[:2], [10, 10])
        self.assertAlmostEqual(times[2], 5, delta=1)

    def test_prefs_are_snapshotted_around_the_call_and_after_the_wait(self):
        snaps = [["p%d" % i] for i in range(5)]
        lab.PW = types.SimpleNamespace(stage="", snapshot=mock.Mock(side_effect=snaps))
        br = FakeBridge({"t": ok_reply("ok")})
        with mock.patch.object(lab.time, "sleep"):
            [e] = self.group([{"id": "s1", "tool": "t", "wait_after": 1}], br)
        self.assertEqual((e["prefs_before"], e["prefs_at_response"], e["prefs_after_wait"]), (["p0"], ["p1"], ["p2"]))
        self.assertEqual(lab.PW.stage, "s1")
        self.group([{"id": "s2", "tool": "t", "batch": "bx"}], br)
        self.assertEqual(lab.PW.stage, "bx")

    def test_a_draft_is_found_by_subject_and_probed(self):
        self.put_drafts(eml(["Subject: Report 42", "X-Probe: needle", "Content-Type: multipart/mixed; boundary=B"],
                            mime("B", "X-Part: needle\r\n\r\nbody")))
        br = FakeBridge({"t": ok_reply("ok")})
        step = {"id": "s1", "tool": "t", "draft_subject": "Report 42",
                "probes": [{"header": "X-Probe", "needle": "needle"}],
                "part_probes": [{"header": "X-Part", "needle": "needle"}]}
        [e] = self.group([step], br)
        self.assertEqual(e["draft"]["folder"], "Mail/Local Folders/Drafts")
        self.assertEqual(e["draft"]["header_names"], ["Subject", "X-Probe", "Content-Type"])
        self.assertEqual([p["status"] for p in e["probes"]], ["OWN_HEADER"])
        self.assertEqual([(m["part"], m["probes"][0]["status"]) for m in e["mime_parts"]], [("1", "OWN_HEADER")])

    def test_a_draft_that_never_appears_is_reported_as_no_draft(self):
        br = FakeBridge({"t": ok_reply("ok")})
        step = {"id": "s1", "tool": "t", "draft_subject": "Nothing", "probes": [{"header": "H", "needle": "n"}]}
        with mock.patch.object(lab, "wait_for", side_effect=lambda pred, timeout, step=0.25: pred() or None) as wf:
            [e] = self.group([step], br)
        self.assertEqual((e["draft"], e["probes"]), (None, [{"header": "H", "needle": "n", "status": "NO_DRAFT"}]))
        self.assertEqual(wf.call_args[0][1:], (30, 0.5))

    def test_the_last_matching_draft_is_used(self):
        self.put_drafts(eml(["Subject: dup", "X-N: 1"]), eml(["Subject: dup", "X-N: 2"]))
        [e] = self.group([{"id": "s1", "tool": "t", "draft_subject": "dup", "draft_wait": 1}],
                         FakeBridge({"t": ok_reply("ok")}))
        self.assertEqual(e["draft"]["header_lines"], ["Subject: dup\r\n", "X-N: 2\r\n"])
        self.assertEqual(e["probes"], [])

    def test_a_lone_lab_http_idle_step_runs_the_pseudo_tool(self):
        step = {"id": "idle", "tool": lab.LAB_HTTP_IDLE, "arguments": {"wait": 0}}
        entries = self.group([step], FakeBridge())
        self.assertEqual(entries, [{"id": "idle", "tool": "labHttpIdle", "arguments": {"wait": 0}, "ok": False,
                                    "error": "labHttpIdle: wait must be an integer 1-120"}])


class HttpIdleTests(LabCase):
    def run_idle(self, server, **args):
        self.addCleanup(server.close)
        self.put(lab.CONN, json.dumps({"port": server.port}))
        st = {"id": "idle", "tool": lab.LAB_HTTP_IDLE, "arguments": args}
        entry = {}
        lab.run_http_idle(st, entry)
        return entry

    def test_it_reports_a_server_that_closes_the_connection(self):
        server = OneShotServer("close")
        e = self.run_idle(server, wait=5, send="POST / HTTP/1.1\r\n")
        text = json.loads(e["text"])
        self.assertEqual((e["ok"], text["closed"], text["received"]), (True, True, ""))
        self.assertLess(text["closedAfter"], 5)
        self.assertEqual(server.received, b"POST / HTTP/1.1\r\n")
        self.assertEqual(lab.FULLTEXT["idle"], e["text"])
        self.assertIn("sent_at", e)
        self.assertIn("answered_at", e)

    def test_it_sends_a_request_start_by_default_and_returns_the_first_bytes_received(self):
        server = OneShotServer("reply", b"HTTP/1.1 408 Request Timeout\r\n\r\n")
        e = self.run_idle(server, wait=5)
        self.assertEqual(server.received, b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\n")
        self.assertEqual(json.loads(e["text"])["received"], "HTTP/1.1 408 Request Timeout\r\n\r\n")

    def test_it_counts_a_reset_connection_as_closed(self):
        e = self.run_idle(OneShotServer("reset"), wait=5)
        self.assertIs(json.loads(e["text"])["closed"], True)

    def test_it_reports_a_server_that_keeps_the_connection_open(self):
        e = self.run_idle(OneShotServer("hold"), wait=1)
        text = json.loads(e["text"])
        self.assertEqual((e["ok"], text["closed"], text["received"]), (True, False, ""))
        self.assertGreaterEqual(text["closedAfter"], 1.0)

    def test_it_refuses_invalid_arguments(self):
        for args, message in (({"wait": 0}, "wait must be an integer 1-120"),
                              ({"wait": 121}, "wait must be an integer 1-120"),
                              ({"wait": True}, "wait must be an integer 1-120"),
                              ({"wait": "5"}, "wait must be an integer 1-120"),
                              ({"send": 5}, "send must be a string of at most 4096 characters"),
                              ({"send": "x" * 4097}, "send must be a string of at most 4096 characters")):
            e = {}
            lab.run_http_idle({"id": "i", "arguments": args}, e)
            self.assertEqual((e["ok"], e["error"]), (False, "labHttpIdle: " + message))

    def test_it_reports_a_missing_connection_file_and_a_refused_connection(self):
        e = {}
        lab.run_http_idle({"id": "i", "arguments": {}}, e)
        self.assertEqual(e["ok"], False)
        self.assertTrue(e["error"].startswith("labHttpIdle: no connection file: "))
        self.put(lab.CONN, '{"port": 1}')
        e = {}
        with mock.patch.object(lab.socket, "create_connection", side_effect=ConnectionRefusedError("refused")):
            lab.run_http_idle({"id": "i", "arguments": {}}, e)
        self.assertEqual((e["ok"], e["error"]), (False, "labHttpIdle: refused"))
        self.assertNotIn("idle", lab.FULLTEXT)

    def test_it_reports_a_connection_file_without_a_valid_port_as_a_failed_step(self):
        for content, shown in (("{}", "None"), ('{"port": "8000"}', "'8000'"), ('{"port": 0}', "0"),
                               ('{"port": true}', "True")):
            with self.subTest(content=content):
                self.put(lab.CONN, content)
                e = {}
                lab.run_http_idle({"id": "i", "arguments": {}}, e)
                self.assertEqual((e["ok"], e["error"]),
                                 (False, "labHttpIdle: no valid port in the connection file: " + shown))


# ------------------------------------------------------------------------------------------------ inspection
class InspectTests(LabCase):
    def mailpit(self, messages):
        raws = {"m%d" % i: raw for i, (_, raw) in enumerate(messages)}

        def fake_http(method, url, timeout=10):
            return 200, raws[url.split("/")[-2]]

        def fake_json(url, timeout=10):
            return {"total": len(messages), "messages": [
                {"ID": "m%d" % i, "Subject": s, "From": {"Address": "f@x"}, "To": [], "Cc": None, "Bcc": []}
                for i, (s, _) in enumerate(messages)]}
        return mock.patch.multiple(lab, http=fake_http, http_json=fake_json)

    def test_inspect_records_drafts_mbox_sizes_and_mailpit_messages(self):
        self.put_drafts(eml(["Subject: Draft"], "b"))
        inbox = self.put(lab.PROF + "/Mail/Local Folders/Inbox", b"From - Thu Jan 01 00:00:00 2026\n\nx")
        crlf = b"Subject: One\r\nFrom: a@x\r\n\r\nbody one"
        lf = b"Subject: Two\n\nbody two"
        with self.mailpit([("One", crlf), ("Two", lf)]):
            lab.inspect({})
        self.assertEqual([d["folder"] for d in lab.R["drafts"]], ["Mail/Local Folders/Drafts"])
        self.assertEqual(lab.R["mbox_folders"]["Mail/Local Folders/Inbox"], os.path.getsize(inbox))
        mp = lab.R["mailpit"]
        self.assertEqual(mp["total"], 2)
        self.assertEqual([(m["ID"], m["Subject"], m["raw_headers"]) for m in mp["messages"]],
                         [("m0", "One", "Subject: One\r\nFrom: a@x"), ("m1", "Two", "Subject: Two")])
        self.assertEqual(mp["messages"][0]["From"], {"Address": "f@x"})
        self.assertEqual([(m["mailpit_subject"], m["body"]) for m in lab.MAILPIT_FULL],
                         [("One", b"body one"), ("Two", b"body two")])

    def test_inspect_keeps_the_error_when_mailpit_cannot_be_read(self):
        with mock.patch.object(lab, "http_json", side_effect=OSError("mailpit is down")):
            lab.inspect({})
        self.assertEqual(lab.R["mailpit"], {"error": "mailpit is down"})

    def test_inspect_reads_profile_files_and_parses_filter_rules(self):
        rules = 'version="9"\nname="Rule"\nenabled="yes"\n'
        self.put(lab.PROF + "/Mail/Local Folders/msgFilterRules.dat", rules)
        self.put(lab.PROF + "/notes.txt", b"caf\xc3\xa9 \xff")
        os.makedirs(lab.PROF + "/adir")
        os.symlink(self.tmp + "/conn.json", lab.PROF + "/link.txt")
        files = ["Mail/Local Folders/msgFilterRules.dat", "notes.txt", "missing.txt", "adir", "link.txt", "../outside"]
        with mock.patch.object(lab, "http_json", side_effect=OSError("down")):
            lab.inspect({"inspect": {"files": files}})
        got = lab.R["files"]
        self.assertEqual(got["notes.txt"], {"bytes": 7, "sha256": hashlib.sha256(b"caf\xc3\xa9 \xff").hexdigest(),
                                            "content": "caf\u00e9 \ufffd"})
        self.assertEqual(lab.FILES_FULL["notes.txt"], "caf\u00e9 \ufffd")
        self.assertEqual(got["link.txt"], {"error": "a link, or outside the profile"})
        self.assertEqual(got["../outside"], {"error": "a link, or outside the profile"})
        self.assertTrue(got["missing.txt"]["error"].startswith("[Errno 2]"))
        self.assertTrue(got["adir"]["error"].startswith("[Errno 21]"))
        parsed = lab.R["filter_rules"]["Mail/Local Folders/msgFilterRules.dat"]
        self.assertEqual((parsed["header"], parsed["rules"][0]["name"]), ({"version": "9"}, "Rule"))
        self.assertEqual(list(lab.R["filter_rules"]), ["Mail/Local Folders/msgFilterRules.dat"])

    def inspect_prefs(self, tb):
        lab.PROCS["thunderbird"] = tb
        with mock.patch.object(lab, "http_json", side_effect=OSError("down")):
            lab.inspect({"inspect": {"prefs": ["mail.", "extensions."]}})

    def test_inspect_stops_thunderbird_cleanly_then_reads_the_wanted_prefs(self):
        self.put(lab.PROF + "/prefs.js",
                 'user_pref("mail.a", 1);\nuser_pref("x.b", 2);\nuser_pref("extensions.c", true);\n')
        tb = FakeProc(code=None)
        self.inspect_prefs(tb)
        self.assertEqual(tb.signals, [lab.signal.SIGTERM])
        self.assertEqual(lab.R["prefs_js"], ['user_pref("mail.a", 1);', 'user_pref("extensions.c", true);'])

    def test_inspect_goes_on_when_thunderbird_does_not_stop_in_time(self):
        self.put(lab.PROF + "/prefs.js", 'user_pref("mail.a", 1);\n')
        tb = FakeProc(code=None, wait_error=subprocess.TimeoutExpired("tb", 20))
        self.inspect_prefs(tb)
        self.assertEqual(lab.R["prefs_js"], ['user_pref("mail.a", 1);'])

    def test_inspect_does_not_signal_a_thunderbird_that_already_exited(self):
        self.put(lab.PROF + "/prefs.js", "")
        tb = FakeProc(code=0)
        self.inspect_prefs(tb)
        self.assertEqual((tb.signals, lab.R["prefs_js"]), ([], []))

    def test_inspect_reports_an_unreadable_prefs_js(self):
        self.inspect_prefs(FakeProc(code=0))
        [line] = lab.R["prefs_js"]
        self.assertTrue(line.startswith("prefs.js unreadable: "))

    def test_inspect_stops_the_prefs_watch_with_the_stage_it_reached(self):
        for prefixes, stage in (([], "inspection"), (["mail."], "after_thunderbird_exit")):
            lab.PW = types.SimpleNamespace(stage="", stop=mock.Mock())
            self.put(lab.PROF + "/prefs.js", "")
            with mock.patch.object(lab, "http_json", side_effect=OSError("down")):
                lab.inspect({"inspect": {"prefs": prefixes}})
            self.assertEqual(lab.PW.stage, stage)
            lab.PW.stop.assert_called_once_with()


class FilterRuleParsingTests(LabCase):
    def test_parse_filter_rules_reads_the_header_the_rules_and_their_actions(self):
        text = ('version="9"\r\nlogging="no"\r\nname="Rule A"\r\nenabled="yes"\r\ntype="17"\r\n'
                'action="Move to folder"\r\nactionValue="mailbox://x/Archive"\r\n'
                'action="Custom"\r\ncustomId="a@b"\r\nactionValue="v"\r\n'
                'condition="AND (subject,contains,\\"q\\")"\r\n'
                'garbage line\r\nname="Rule B"\r\nactionValue="early"\r\nenabled="no"\r\n')
        out = lab.parse_filter_rules(text)
        self.assertEqual(out["header"], {"version": "9", "logging": "no"})
        a, b = out["rules"]
        self.assertEqual(a, {"name": "Rule A", "enabled": "yes", "type": "17",
                             "condition": 'AND (subject,contains,"q")',
                             "actions": [{"action": "Move to folder", "actionValue": "mailbox://x/Archive"},
                                         {"action": "Custom", "customId": "a@b", "actionValue": "v"}]})
        self.assertEqual(b, {"name": "Rule B", "actions": [], "actionValue": "early", "enabled": "no"})

    def test_parse_filter_rules_of_empty_text_and_the_cap_of_200_rules(self):
        self.assertEqual(lab.parse_filter_rules(""), {"header": {}, "rules": []})
        text = "".join('name="r%d"\n' % i for i in range(250))
        self.assertEqual(len(lab.parse_filter_rules(text)["rules"]), 200)


# ------------------------------------------------------------------------------------------------ checks
class FilterCheckTests(LabCase):
    ACCOUNTS = [{"accountId": "a1", "filters": [
        {"name": "F", "index": 0, "enabled": True, "type": 17,
         "terms": [{"attrib": "subject", "op": "contains", "value": "x", "extra": 1}],
         "actions": [{"type": "move", "value": "folderX"}, {"type": "markRead"}]},
        {"name": "Dup"}, "junk"]}, {"accountId": "a2", "filters": [{"name": "Dup"}]}, "not a dict"]

    def listed(self, **c):
        lab.FULLTEXT["lf"] = json.dumps(self.ACCOUNTS)
        return lab.check_listed_filter(dict({"step": "lf", "name": "F"}, **c))

    def test_values_match_uses_none_as_a_wildcard(self):
        self.assertTrue(lab._values_match(["a", None], ["a", "anything"]))
        self.assertFalse(lab._values_match(["a"], ["a", "b"]))
        self.assertFalse(lab._values_match(["a", "b"], ["a", "c"]))
        self.assertTrue(lab._values_match([], []))

    def test_a_listed_filter_passes_when_every_requested_property_matches(self):
        ok, detail = self.listed(action_types=["move", "markRead"], action_values=["folderX", None], enabled=True,
                                 terms=[{"attrib": "subject", "op": "contains", "value": "x"}])
        self.assertTrue(ok)
        self.assertEqual(json.loads(detail)["actions"][0], {"type": "move", "value": "folderX"})
        self.assertEqual(list(json.loads(detail)), ["index", "enabled", "type", "terms", "actions"])

    def test_a_listed_filter_fails_on_each_property_that_differs(self):
        for c in ({"action_types": ["move"]}, {"action_values": ["other", None]}, {"action_values": ["folderX"]},
                  {"enabled": False}, {"terms": [{"attrib": "subject", "op": "is", "value": "x"}]}, {"terms": []}):
            with self.subTest(c=c):
                self.assertFalse(self.listed(**c)[0])

    def test_a_listed_filter_must_be_found_exactly_once(self):
        self.assertEqual(self.listed(name="Missing"), (False, "0 filter(s) named 'Missing'"))
        self.assertEqual(self.listed(name="Dup"), (False, "2 filter(s) named 'Dup'"))
        self.assertEqual(self.listed(name="Dup", account="a2")[0], True)
        self.assertEqual(self.listed(name="F", account="a2"), (False, "0 filter(s) named 'F'"))

    def test_a_listed_filter_can_be_expected_to_be_absent(self):
        self.assertEqual(self.listed(name="Missing", absent=True), (True, "0 filter(s) named 'Missing'"))
        self.assertEqual(self.listed(absent=True), (False, "1 filter(s) named 'F'"))

    def test_a_single_account_object_is_accepted_and_unreadable_text_is_reported(self):
        lab.FULLTEXT["lf"] = json.dumps(self.ACCOUNTS[0])
        self.assertTrue(lab.check_listed_filter({"step": "lf", "name": "F"})[0])
        lab.FULLTEXT["lf"] = "no json"
        self.assertEqual(lab.check_listed_filter({"step": "lf", "name": "F"}),
                         (False, "listFilters response unreadable"))
        self.assertEqual(lab.check_listed_filter({"step": "unknown", "name": "F"}),
                         (False, "listFilters response unreadable"))

    def rules(self):
        lab.R["filter_rules"] = {"f.dat": lab.parse_filter_rules(
            'name="R1"\nenabled="yes"\ntype="17"\naction="Move"\nactionValue="mailbox://x"\naction="Mark read"\n'
            'condition="AND (subject,contains,x)"\nname="R2"\ncondition="AND (subject,contains,x)"\n'
            'name="R3"\ncondition="OR (from,is,y)"\nname="R3"\n')}

    def rule(self, **c):
        self.rules()
        return lab.check_filter_rule(dict({"file": "f.dat", "name": "R1"}, **c))

    def test_a_filter_rule_passes_when_every_requested_property_matches(self):
        ok, detail = self.rule(actions=["Move", "Mark read"], action_values=["mailbox://x", None],
                               condition="AND (subject,contains,x)", condition_contains="subject", enabled="yes",
                               filter_type=17, condition_same_as="R2")
        self.assertTrue(ok)
        self.assertEqual(json.loads(detail)["name"], "R1")

    def test_a_filter_rule_fails_on_each_property_that_differs(self):
        for c in ({"actions": ["Move"]}, {"action_values": ["other", None]}, {"condition": "nope"},
                  {"condition_contains": "absent"}, {"condition_same_as": "R3"}, {"condition_same_as": "Missing"},
                  {"enabled": "no"}, {"filter_type": 18}):
            with self.subTest(c=c):
                self.assertFalse(self.rule(**c)[0])

    def test_a_filter_rule_must_be_found_exactly_once_or_be_absent(self):
        self.assertEqual(self.rule(name="Missing"), (False, "0 rule(s) named 'Missing'"))
        self.assertEqual(self.rule(name="R3"), (False, "2 rule(s) named 'R3'"))
        self.assertEqual(self.rule(name="Missing", absent=True), (True, "0 rule(s) named 'Missing'"))
        self.assertEqual(self.rule(absent=True), (False, "1 rule(s) named 'R1'"))
        self.assertEqual(lab.check_filter_rule({"file": "unknown.dat", "name": "R1", "absent": True})[0], True)


class EvaluateTests(LabCase):
    def setUp(self):
        super().setUp()
        self.put_drafts(eml(["Subject: Alpha draft", "X-Id: 1"], "alpha body"),
                        eml(["Subject: Beta draft", "X-Id: 2"], "beta body"))
        lab.R.update(tools=["listFolders"], mailpit={"total": 2}, prefs_js=['user_pref("mail.a", 1);'])
        lab.R["phases"]["mcp"] = {"schemas": {"listFolders": ["accountId"]}}
        lab.TOOLS["listFolders"] = {"description": "Lists folders", "inputSchema": {"properties": {
            "accountId": {"type": "string"}}}}
        lab.FULLTEXT["s1"] = "found: needle"
        lab.FILES_FULL["a.txt"] = "file needle"
        lab.MAILPIT_FULL.append(dict(lab.parse_raw(b"Subject: Mail S\r\nX-Tag: tagged\r\n\r\nraw body"),
                                     mailpit_subject="Mail S"))
        self.steps = {"s1": {"ok": True, "text": "good"}, "s2": {"ok": False, "error": "bad thing", "text": "t"}}

    def ev(self, **c):
        return lab.evaluate(c, self.steps)

    def test_cold_start_compares_each_given_key_with_the_first_answer(self):
        self.assertEqual(self.ev(type="cold_start", answered=True), (False, "no cold start was run"))
        lab.R["cold_start"] = {"mode": True, "capabilities": {"tools": {"listChanged": True}}, "tools": 0,
                               "seconds": 20.0, "list_changed_after": 2.0}
        self.assertTrue(self.ev(type="cold_start", answered=True, tools=0, list_changed=True,
                                capability_list_changed=True, max_seconds=25)[0])
        for wrong in ({"answered": False}, {"tools": 41}, {"list_changed": False}, {"capability_list_changed": False},
                      {"max_seconds": 10}, {"min_tools": 1}):
            with self.subTest(wrong=wrong):
                ok, detail = self.ev(type="cold_start", **wrong)
                self.assertFalse(ok)
                self.assertTrue(detail.startswith("wrong: %s; " % next(iter(wrong))))
        lab.R["cold_start"] = {"mode": 10, "capabilities": {"tools": {}}, "tools": None, "seconds": None,
                               "list_changed_after": None, "error": "boom"}
        self.assertTrue(self.ev(type="cold_start", answered=False, list_changed=False, capability_list_changed=False)[0])
        self.assertFalse(self.ev(type="cold_start", max_seconds=60)[0])
        self.assertFalse(self.ev(type="cold_start", min_tools=0)[0])
        lab.R["cold_start"] = {"capabilities": "odd", "tools": 41}
        self.assertTrue(self.ev(type="cold_start", min_tools=10, capability_list_changed=False)[0])

    def test_tool_ok_needs_a_successful_step(self):
        self.assertEqual(self.ev(type="tool_ok", step="s1"), (True, "good"))
        ok, detail = self.ev(type="tool_ok", step="s2")
        self.assertFalse(ok)
        self.assertEqual(json.loads(detail)["error"], "bad thing")
        self.assertEqual(self.ev(type="tool_ok", step="none"), (False, "null"))

    def test_tools_list_has_checks_presence_or_absence(self):
        self.assertEqual(self.ev(type="tools_list_has", tool="listFolders"), (True, "listed=True"))
        self.assertEqual(self.ev(type="tools_list_has", tool="other"), (False, "listed=False"))
        self.assertEqual(self.ev(type="tools_list_has", tool="other", present=False), (True, "listed=False"))

    def test_draft_checks_find_drafts_by_subject(self):
        self.assertEqual(self.ev(type="draft_exists", subject="Alpha"), (True, ""))
        self.assertEqual(self.ev(type="draft_exists", subject="Gamma"), (False, ""))
        self.assertEqual(self.ev(type="draft_count", subject="draft", equals=2), (True, "2 draft(s)"))
        self.assertEqual(self.ev(type="draft_count", subject="draft", equals=1), (False, "2 draft(s)"))
        self.assertEqual(self.ev(type="draft_header_contains", subject="Beta", header="x-id", contains="2"),
                         (True, "2"))
        for c in ({"subject": "Beta", "header": "X-Id", "contains": "1"}, {"subject": "Beta", "header": "X-No",
                                                                          "contains": "2"},
                  {"subject": "Gamma", "header": "X-Id", "contains": "2"}):
            self.assertEqual(self.ev(type="draft_header_contains", **c), (False, ""))
        self.assertEqual(self.ev(type="draft_body_contains", subject="Alpha", contains="alpha body"),
                         (True, "1 matching draft(s)"))
        self.assertEqual(self.ev(type="draft_body_contains", subject="Alpha", contains="beta"),
                         (False, "0 matching draft(s)"))

    def test_schema_and_mailpit_total(self):
        self.assertTrue(self.ev(type="schema_has", tool="listFolders", property="accountId")[0])
        self.assertFalse(self.ev(type="schema_has", tool="listFolders", property="other")[0])
        self.assertFalse(self.ev(type="schema_has", tool="unknown", property="accountId")[0])
        self.assertEqual(self.ev(type="mailpit_total", equals=2), (True, "total=2"))
        self.assertEqual(self.ev(type="mailpit_total", equals=3), (False, "total=2"))

    def test_tool_error_contains_needs_a_failed_step_with_the_text(self):
        self.assertEqual(self.ev(type="tool_error_contains", step="s2", contains="bad"), (True, "bad thing | t"))
        self.assertFalse(self.ev(type="tool_error_contains", step="s2", contains="other")[0])
        self.assertFalse(self.ev(type="tool_error_contains", step="s1", contains="good")[0])
        self.assertFalse(self.ev(type="tool_error_contains", step="none", contains="x")[0])

    def test_prefs_js_line_can_expect_a_line_or_its_absence(self):
        self.assertEqual(self.ev(type="prefs_js_line", contains="mail.a"), (True, ['user_pref("mail.a", 1);']))
        self.assertEqual(self.ev(type="prefs_js_line", contains="mail.zz"), (False, []))
        self.assertEqual(self.ev(type="prefs_js_line", contains="mail.zz", present=False), (True, []))
        self.assertEqual(self.ev(type="prefs_js_line", contains="mail.a", present=False)[0], False)

    def test_tool_json_checks_use_a_dotted_path_in_the_tool_definition(self):
        base = {"tool": "listFolders", "path": "inputSchema.properties.accountId.type"}
        self.assertEqual(self.ev(type="tool_json_equals", equals="string", **base), (True, '"string"'))
        self.assertFalse(self.ev(type="tool_json_equals", equals="number", **base)[0])
        tj = dict(type="tool_json_contains", tool="listFolders")
        self.assertTrue(self.ev(path="description", contains="folders", **tj)[0])
        self.assertTrue(self.ev(path="inputSchema", contains="accountId", **tj)[0])
        self.assertFalse(self.ev(path="description", contains="x", **tj)[0])
        self.assertEqual(self.ev(type="tool_json_equals", equals=1, tool="listFolders", path="no.such"),
                         (False, "path not found"))
        self.assertTrue(self.ev(contains="Lists", **tj)[0])

    def test_tool_result_contains_needs_a_successful_step_and_the_expected_presence(self):
        self.assertEqual(self.ev(type="tool_result_contains", step="s1", contains="needle"),
                         (True, "present=True; found: needle"))
        self.assertFalse(self.ev(type="tool_result_contains", step="s1", contains="other")[0])
        self.assertTrue(self.ev(type="tool_result_contains", step="s1", contains="other", present=False)[0])
        self.assertFalse(self.ev(type="tool_result_contains", step="s2", contains="x", present=False)[0])

    def test_file_contains_needs_an_inspected_file(self):
        self.assertEqual(self.ev(type="file_contains", file="a.txt", contains="needle"), (True, "present=True"))
        self.assertEqual(self.ev(type="file_contains", file="a.txt", contains="zzz"), (False, "present=False"))
        self.assertEqual(self.ev(type="file_contains", file="a.txt", contains="zzz", present=False)[0], True)
        self.assertEqual(self.ev(type="file_contains", file="b.txt", contains="x"), (False, "file missing"))
        self.assertEqual(self.ev(type="file_contains", file="b.txt", contains="x", present=False)[0], False)

    def test_mailpit_checks_look_at_the_captured_messages_by_subject(self):
        self.assertEqual(self.ev(type="mailpit_header_contains", subject="Mail", header="X-Tag", contains="tag"),
                         (True, '["tagged"]'))
        self.assertEqual(self.ev(type="mailpit_header_contains", subject="Mail", header="X-Tag", contains="zzz"),
                         (False, '["tagged"]'))
        self.assertEqual(self.ev(type="mailpit_header_contains", subject="Other", header="X-Tag", contains="tag"),
                         (False, "[]"))
        self.assertEqual(self.ev(type="mailpit_body_contains", subject="Mail", contains="raw body"),
                         (True, "1 matching message(s)"))
        self.assertEqual(self.ev(type="mailpit_body_contains", subject="Mail", contains="nope"),
                         (False, "0 matching message(s)"))

    def test_the_filter_and_attachment_checks_are_dispatched(self):
        lab.FULLTEXT["lf"] = json.dumps([{"accountId": "a", "filters": [{"name": "F"}]}])
        lab.R["filter_rules"] = {"f.dat": {"rules": [{"name": "R", "actions": []}]}}
        self.assertTrue(self.ev(type="listed_filter", step="lf", name="F")[0])
        self.assertTrue(self.ev(type="filter_rule", file="f.dat", name="R")[0])
        self.assertEqual(self.ev(type="draft_attachment", subject="Alpha", filename="a.bin", bytes=1)[0], False)

    def test_an_unknown_check_type_fails(self):
        self.assertEqual(self.ev(type="nonsense"), (False, "unknown check type"))
        self.assertEqual(lab.evaluate({}, self.steps), (False, "unknown check type"))

    def test_tool_path_walks_the_tool_definition(self):
        self.assertEqual(lab.tool_path("listFolders", "inputSchema.properties.accountId"), (True, {"type": "string"}))
        self.assertEqual(lab.tool_path("listFolders", ""), (True, lab.TOOLS["listFolders"]))
        self.assertEqual(lab.tool_path("listFolders", None), (True, lab.TOOLS["listFolders"]))
        self.assertEqual(lab.tool_path("listFolders", "description.deeper"), (False, None))
        self.assertEqual(lab.tool_path("unknown", "x"), (False, None))

    def test_step_error_text_joins_the_error_fields_that_are_present(self):
        self.assertEqual(lab.step_error_text({"error": "e", "rpc_error": "r", "tool_error": "t", "text": "x"}),
                         "e | r | t | x")
        self.assertEqual(lab.step_error_text({"text": "only", "error": ""}), "only")
        self.assertEqual(lab.step_error_text({}), "")

    def test_mailpit_matching_selects_by_subject_substring(self):
        self.assertEqual(len(lab.mailpit_matching("Mail")), 1)
        self.assertEqual(lab.mailpit_matching("Absent"), [])


class AttachmentCheckTests(LabCase):
    PAYLOAD = base64.b64encode(b"a" * 5).decode()

    def check(self, **c):
        return lab.check_draft_attachment(dict({"subject": "Att", "filename": "a.bin"}, **c))

    def test_a_base64_attachment_is_decoded_and_measured(self):
        payload = self.PAYLOAD[:4] + "\r\n" + self.PAYLOAD[4:]
        self.put_drafts(attachment_draft("Att", "a.bin", payload))
        ok, detail = self.check(bytes=5, byte=97)
        self.assertTrue(ok)
        self.assertEqual(detail, "5 bytes decoded (CTE base64), sha256 " + hashlib.sha256(b"aaaaa").hexdigest())
        self.assertTrue(self.check(bytes=5)[0])

    def test_the_size_and_the_byte_value_must_match(self):
        self.put_drafts(attachment_draft("Att", "a.bin", self.PAYLOAD))
        self.assertFalse(self.check(bytes=6)[0])
        self.assertFalse(self.check(bytes=5, byte=98)[0])

    def test_an_attachment_without_an_encoding_is_measured_as_is(self):
        self.put_drafts(attachment_draft("Att", "a.bin", "xyz", cte="7bit"))
        ok, detail = self.check(bytes=5)
        self.assertTrue(ok)
        self.assertTrue(detail.startswith("5 bytes decoded (CTE 7bit)"))

    def test_a_missing_attachment_is_reported(self):
        self.put_drafts(attachment_draft("Att", "a.bin", self.PAYLOAD), attachment_draft("Other", "b.bin", "x"))
        self.assertEqual(self.check(filename="nope.bin", bytes=1),
                         (False, "attachment 'nope.bin' not found in drafts 'Att'"))
        self.assertEqual(self.check(subject="Other", filename="a.bin", bytes=1)[0], False)
        self.assertEqual(self.check(subject="Nothing", bytes=1)[0], False)


class RunChecksTests(LabCase):
    def test_run_checks_counts_pass_fail_xfail_and_xpass(self):
        lab.R["steps"] = [{"id": "s1", "ok": True, "text": "t"}, {"id": "s2", "ok": False}]
        sc = {"checks": [{"type": "tool_ok", "step": "s1"}, {"type": "tool_ok", "step": "s2"},
                         {"type": "tool_ok", "step": "s2", "xfail": "known bug"},
                         {"type": "tool_ok", "step": "s1", "xfail": "fixed meanwhile"}]}
        self.assertFalse(lab.run_checks(sc))
        self.assertEqual(lab.R["summary"], {"pass": 1, "fail": 1, "xfail": 1, "xpass": 1})
        self.assertEqual([c["status"] for c in lab.R["checks"]], ["pass", "fail", "xfail", "xpass"])
        self.assertEqual([c["ok"] for c in lab.R["checks"]], [True, False, False, True])
        self.assertEqual(lab.R["checks"][0]["check"], sc["checks"][0])

    def test_run_checks_passes_when_only_expected_failures_fail(self):
        lab.R["steps"] = [{"id": "s2", "ok": False}]
        self.assertTrue(lab.run_checks({"checks": [{"type": "tool_ok", "step": "s2", "xfail": "known"}]}))

    def test_run_checks_turns_an_exception_into_a_failed_check(self):
        lab.R["steps"] = [{"id": "s1", "ok": True}]
        self.assertFalse(lab.run_checks({"checks": [{"type": "tool_ok", "step": ["unhashable"]}]}))
        self.assertEqual(lab.R["checks"][0]["detail"], "exception: unhashable type: 'list'")

    def test_run_checks_clips_the_detail(self):
        lab.R["steps"] = [{"id": "s1", "ok": True, "text": "z" * 900}]
        lab.run_checks({"checks": [{"type": "tool_ok", "step": "s1"}]})
        self.assertEqual(len(lab.R["checks"][0]["detail"]), 500 + len("...[truncated: 900 characters in total]"))


# ------------------------------------------------------------------------------------------------ output and main
class EmitTests(LabCase):
    def emit(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lab.emit()
        return out.getvalue()

    def test_emit_writes_one_json_object_between_the_nonce_markers(self):
        lab.NONCE = NONCE
        lab.R["tools"] = ["a"]
        self.put(lab.LOGS + "/bridge.log", "b1\nb2\n")
        text = self.emit()
        self.assertTrue(text.startswith(lab.MARK_BEGIN % NONCE + "\n"))
        self.assertTrue(text.endswith("\n" + lab.MARK_END % NONCE + "\n"))
        res = result_block(text)
        self.assertEqual(res["harness"], lab.HARNESS)
        self.assertEqual(res["logs"], {"thunderbird": None, "bridge": "b1\nb2", "mailpit": None, "xvfb": None})
        self.assertIsInstance(res["duration_s"], float)
        self.assertTrue(text.isascii())

    def test_emit_escapes_non_ascii_text(self):
        lab.NONCE = NONCE
        lab.R["errors"].append("caf\u00e9\r\nline")
        text = self.emit()
        self.assertIn("caf\\u00e9\\r\\nline", text)
        self.assertEqual(result_block(text)["errors"], ["caf\u00e9\r\nline"])

    def test_emit_drops_the_logs_and_shortens_the_tool_list_when_the_result_is_too_big(self):
        lab.NONCE = NONCE
        lab.R["tools"] = ["t%d" % i for i in range(10)]
        self.put(lab.LOGS + "/bridge.log", "x" * 3000)
        with mock.patch.object(lab, "MAX_OUT", 1500):
            res = result_block(self.emit())
        self.assertEqual(res["logs"], {"truncated": True})
        self.assertEqual(res["tools"], ["t0", "t1", "t2", "t3", "t4", "..."])

    def test_emit_keeps_only_the_summary_of_a_result_that_is_still_too_big_as_valid_json(self):
        lab.NONCE = NONCE
        lab.R.update(ok=True, big="x" * 3000, summary={"pass": 2, "fail": 0}, scenario={"name": "n"})
        lab.R["errors"].append("e" * 900)
        with mock.patch.object(lab, "MAX_OUT", 1500):
            res = result_block(self.emit())
        self.assertEqual((res["ok"], res["summary"], res["scenario"]), (False, {"pass": 2, "fail": 0}, {"name": "n"}))
        self.assertNotIn("big", res)
        self.assertEqual(len(res["errors"]), 2)
        self.assertLess(len(res["errors"][0]), 600)
        self.assertEqual(res["errors"][1], "the result was larger than 1500 characters: only its summary is kept")


class MainTests(LabCase):
    def run_main(self, raw=None, as_init=False, **stages):
        """main() with every stage faked; `stages` replaces some of them. Returns (status, stdout, calls)."""
        calls = []

        def stage(name, result=None):
            def fn(*a, **k):
                calls.append(name)
                return result
            return fn

        def read_header():
            calls.append("read_header")
            lab.NONCE, lab.SCENARIO_RAW = NONCE, raw if raw is not None else json.dumps(SCENARIO).encode()

        def run_mcp(sc, br, init=None):
            calls.append("run_mcp" if init is None else "run_mcp after " + init)
            lab.R["steps"] = [{"id": "s1", "ok": True, "text": "t"}]
        fakes = {"run_as_init": stage("run_as_init", as_init), "read_header": read_header,
                 "extract_source": stage("extract_source"), "build_xpi": stage("build_xpi", ("id@x.y", "1.0")),
                 "start_mailpit": stage("start_mailpit"), "start_xvfb": stage("start_xvfb"),
                 "make_profile": stage("make_profile"), "start_tb": stage("start_tb"),
                 "Bridge": lambda env=None: calls.append("Bridge") or ("bridge", env), "run_mcp": run_mcp,
                 "run_cold_start": lambda sc, br: calls.append("run_cold_start") or "cold init",
                 "inspect": stage("inspect"), "stop": lambda name, sig=None: calls.append("stop " + name),
                 "time": types.SimpleNamespace(monotonic=time.monotonic, sleep=lambda s: calls.append("sleep %s" % s))}
        fakes.update(stages)
        out = io.StringIO()
        with mock.patch.multiple(lab, **fakes), contextlib.redirect_stdout(out):
            status = lab.main()
        return status, out.getvalue(), calls

    def test_a_clean_run_runs_every_stage_in_order_and_emits_an_ok_result(self):
        status, out, calls = self.run_main()
        self.assertEqual(status, 0)
        self.assertEqual(calls, ["run_as_init", "read_header", "extract_source", "build_xpi", "start_mailpit",
                                 "start_xvfb", "make_profile", "start_tb", "Bridge", "run_mcp", "inspect",
                                 "stop bridge", "stop thunderbird", "stop xvfb", "stop mailpit"])
        res = result_block(out)
        self.assertEqual((res["ok"], res["errors"], res["pid1"]), (True, [], "not the harness"))
        self.assertEqual(res["extension"], {"id": "id@x.y", "version": "1.0"})
        self.assertEqual(res["scenario"]["name"], "n")
        self.assertEqual(res["scenario"]["sha256"], hashlib.sha256(json.dumps(SCENARIO).encode()).hexdigest())
        self.assertEqual(res["summary"], {"pass": 1, "fail": 0, "xfail": 0, "xpass": 0})
        self.assertTrue(any(l.startswith("warning: PID 1 is not the harness") for l in self.logs))

    def test_with_bridge_first_the_bridge_starts_before_thunderbird(self):
        sc = dict(SCENARIO, bridge_first=True)
        status, _, calls = self.run_main(raw=json.dumps(sc).encode())
        self.assertEqual(status, 0)
        self.assertEqual(calls[calls.index("make_profile") + 1:calls.index("inspect")],
                         ["Bridge", "run_cold_start", "run_mcp after cold init"])

    def test_running_as_the_pid_1_init_child_is_recorded_without_a_warning(self):
        status, out, _ = self.run_main(as_init=True)
        self.assertEqual((status, result_block(out)["pid1"]), (0, "harness init"))
        self.assertFalse(any(l.startswith("warning") for l in self.logs))

    def test_the_bridge_receives_the_scenario_environment(self):
        sc = dict(SCENARIO, bridge_env={"THUNDERBIRD_MCP_A": "1"})
        seen = []
        self.run_main(raw=json.dumps(sc).encode(), Bridge=lambda env=None: seen.append(env))
        self.assertEqual(seen, [{"THUNDERBIRD_MCP_A": "1"}])

    def test_a_stage_that_raises_is_reported_and_the_cleanup_still_runs(self):
        def boom():
            raise RuntimeError("no mailpit")
        status, out, calls = self.run_main(start_mailpit=boom)
        self.assertEqual(status, 1)
        res = result_block(out)
        self.assertEqual((res["ok"], res["errors"]), (False, ["RuntimeError: no mailpit"]))
        self.assertNotIn("start_xvfb", calls)
        self.assertEqual(calls[-4:], ["stop bridge", "stop thunderbird", "stop xvfb", "stop mailpit"])

    def test_a_header_error_is_reported_through_the_result(self):
        def bad_header():
            raise ValueError("scenario length line missing or invalid")
        status, out, calls = self.run_main(read_header=bad_header)
        self.assertEqual(status, 1)
        self.assertEqual(result_block(out, "")["errors"], ["ValueError: scenario length line missing or invalid"])
        self.assertNotIn("extract_source", calls)

    def test_a_scenario_that_does_not_validate_is_reported(self):
        status, out, calls = self.run_main(raw=b'{"checks": []}')
        self.assertEqual(status, 1)
        self.assertEqual(result_block(out)["errors"], ["ValueError: checks: a list of 1 to 200 checks"])
        self.assertNotIn("extract_source", calls)

    def test_a_failed_check_or_a_recorded_error_makes_the_run_fail(self):
        sc = dict(SCENARIO, checks=[{"type": "tool_ok", "step": "s1"}, {"type": "tools_list_has", "tool": "absent"}])
        status, out, _ = self.run_main(raw=json.dumps(sc).encode())
        self.assertEqual((status, result_block(out)["summary"]["fail"]), (1, 1))
        status, out, _ = self.run_main(start_xvfb=lambda: lab.fail("side error"))
        self.assertEqual((status, result_block(out)["ok"], result_block(out)["summary"]["fail"]), (1, False, 0))

    def test_the_prefs_watch_is_started_and_stopped_when_inspect_did_not_stop_it(self):
        watch = mock.Mock()
        factory = mock.Mock(return_value=watch)
        sc = dict(SCENARIO, watch_prefs=["mail."])
        status, _, _ = self.run_main(raw=json.dumps(sc).encode(), PrefWatch=factory)
        self.assertEqual(status, 0)
        factory.assert_called_once_with(["mail."])
        watch.stop.assert_called_once_with()

    def test_the_prefs_watch_is_not_stopped_twice(self):
        watch = mock.Mock()

        def inspect(sc):
            lab.R["prefs_timeline"] = []
        sc = dict(SCENARIO, watch_prefs=["mail."])
        self.run_main(raw=json.dumps(sc).encode(), PrefWatch=mock.Mock(return_value=watch), inspect=inspect)
        watch.stop.assert_not_called()

    def test_an_instrumented_addon_gets_a_last_write_and_its_coverage_is_read(self):
        self.put(lab.PROF + "/cp-coverage.txt", "0f")
        _, out, calls = self.run_main()
        self.assertIn("sleep 0.6", calls)
        self.assertEqual(result_block(out)["coverage_hex"], "0f")

    def test_a_coverage_file_that_cannot_be_read_is_reported(self):
        _, out, _ = self.run_main(read_coverage=mock.Mock(side_effect=OSError("disk gone")))
        self.assertEqual(result_block(out)["coverage_error"], "disk gone")


# ------------------------------------------------------------------------------------------------ whole harness
class ProcessTests(unittest.TestCase):
    """lab.py run as a subprocess, as the container runs it, with input that stops it before anything is started."""

    @classmethod
    def setUpClass(cls):
        cls.logs_existed = os.path.isdir(REAL_LOGS)

    @classmethod
    def tearDownClass(cls):
        if not cls.logs_existed:                # the harness creates its log directory when it starts
            with contextlib.suppress(OSError):
                os.rmdir(REAL_LOGS)

    def run_lab(self, stdin):
        p = subprocess.run([sys.executable, "-B", "-I", LAB], input=stdin, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=120)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

    def test_a_missing_or_invalid_nonce_stops_with_status_2_and_no_result(self):
        for data in (b"", b"not a nonce\n", NONCE.upper().encode() + b"\n", b"0" * 70 + b"\n",
                     NONCE.encode()[:-1] + b"\n"):
            with self.subTest(data=data[:12]):
                rc, out, _ = self.run_lab(data)
                self.assertEqual(rc, 2)
                self.assertTrue(out.startswith("missing or invalid nonce: stopping\n"))
                self.assertNotIn("RESULT JSON", out)

    def test_an_invalid_length_line_gives_a_failed_result_with_the_error(self):
        for tail, message in ((b"abc\n", "scenario length line missing or invalid"),
                              (b"2000000\n", "scenario length line missing or invalid"),
                              (b"10\nabc", "input ended inside the scenario")):
            with self.subTest(tail=tail):
                rc, out, err = self.run_lab(NONCE.encode() + b"\n" + tail)
                self.assertEqual(rc, 1)
                res = result_block(out)
                self.assertIs(res["ok"], False)
                self.assertEqual(len(res["errors"]), 1)
                self.assertTrue(res["errors"][0].startswith("ValueError: " + message), res["errors"])
                self.assertEqual(res["pid1"], "not the harness")
                self.assertIsInstance(res["nondumpable"], bool)
                self.assertIn("warning: PID 1 is not the harness", err)

    def test_an_invalid_scenario_gives_a_failed_result_naming_the_problem(self):
        raw = b'{"checks": []}'
        rc, out, _ = self.run_lab(NONCE.encode() + b"\n%d\n" % len(raw) + raw + b"tar stream that is never read")
        self.assertEqual(rc, 1)
        res = result_block(out)
        self.assertEqual((res["ok"], res["errors"]), (False, ["ValueError: checks: a list of 1 to 200 checks"]))
        self.assertNotIn("scenario", res)

    def test_a_scenario_of_unknown_keys_is_refused_the_same_way(self):
        raw = b'{"bogus": 1}'
        rc, out, _ = self.run_lab(NONCE.encode() + b"\n%d\n" % len(raw) + raw)
        self.assertEqual(rc, 1)
        self.assertEqual(result_block(out)["errors"], ["ValueError: scenario: unknown key(s) ['bogus']"])


def main():
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    if not result.wasSuccessful():
        print(stream.getvalue())
        print("harness-tests: FAIL - %d failure(s), %d error(s) in %d tests" % (
            len(result.failures), len(result.errors), result.testsRun))
        return 1
    print("harness-tests: PASS - %d tests" % result.testsRun)
    return 0


if __name__ == "__main__":
    sys.exit(main())
