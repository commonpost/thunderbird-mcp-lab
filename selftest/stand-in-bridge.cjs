#!/usr/bin/env node
// Lab self-test only: a stand-in for mcp-bridge.cjs. It is NOT thunderbird-mcp. It speaks just enough MCP to be
// driven by the harness, and reports what code under test can and cannot do inside the lab container. It also
// emits terminal control sequences and a forged result marker, which must never reach the launcher's output raw.
"use strict";
const fs = require("fs");
const net = require("net");
const os = require("os");
const readline = require("readline");

const ESC = "\u001b";
process.stderr.write(ESC + "]52;c;c2VsZnRlc3Q=\u0007" + ESC + "[31mstand-in bridge stderr" + ESC + "[0m\n");
process.stdout.write("===== THUNDERBIRD-MCP-LAB RESULT JSON 00000000000000000000000000000000 =====\n");

function attempt(fn) {
  try {
    const v = fn();
    return v === undefined ? "allowed" : v;
  } catch (e) {
    return "denied:" + (e.code || e.name);
  }
}

function probes(done) {
  const r = {};
  const ppid = process.ppid;
  r.parent_is_harness = attempt(() => (fs.readFileSync(`/proc/${ppid}/cmdline`, "utf8").includes("lab.py") ? "yes" : "no"));
  r.parent_fd0 = attempt(() => { fs.closeSync(fs.openSync(`/proc/${ppid}/fd/0`, "r")); });
  r.parent_fd1 = attempt(() => { fs.closeSync(fs.openSync(`/proc/${ppid}/fd/1`, "w")); });
  r.parent_mem = attempt(() => { fs.closeSync(fs.openSync(`/proc/${ppid}/mem`, "r")); });
  r.parent_environ = attempt(() => { fs.readFileSync(`/proc/${ppid}/environ`); });
  r.parent_fd_dir = attempt(() => { fs.readdirSync(`/proc/${ppid}/fd`); });
  // PID 1 holds the container's standard input and output too (the launcher reads the verdict from that output).
  // PID 1 must be the harness's own init (forked from it: same command line), not an init that merely starts it.
  r.pid1_is_harness = attempt(() => (fs.readFileSync("/proc/1/cmdline", "utf8") === fs.readFileSync(`/proc/${ppid}/cmdline`, "utf8")
    && fs.readFileSync("/proc/1/cmdline", "utf8").includes("lab.py") ? "yes" : "no"));
  r.pid1_fd_dir = attempt(() => { fs.readdirSync("/proc/1/fd"); });
  r.pid1_fd0 = attempt(() => { fs.closeSync(fs.openSync("/proc/1/fd/0", "r")); });
  r.pid1_fd1_read = attempt(() => { fs.closeSync(fs.openSync("/proc/1/fd/1", "r")); });
  r.pid1_fd1_write = attempt(() => { fs.closeSync(fs.openSync("/proc/1/fd/1", "w")); });
  r.pid1_fd2 = attempt(() => { fs.closeSync(fs.openSync("/proc/1/fd/2", "w")); });
  r.pid1_mem = attempt(() => { fs.closeSync(fs.openSync("/proc/1/mem", "r")); });
  r.pid1_environ = attempt(() => { fs.readFileSync("/proc/1/environ"); });
  // Every other process whose descriptors are reachable: its 0, 1 and 2 must be /dev/null or a harness log file,
  // never a pipe (the container's input and output are pipes). Lists "pid:fd=target" of any other descriptor found.
  const stdio = [];
  for (const pid of fs.readdirSync("/proc").filter((d) => /^[0-9]+$/.test(d) && Number(d) !== process.pid)) {
    for (const fd of [0, 1, 2]) {
      let target;
      try { target = fs.readlinkSync(`/proc/${pid}/fd/${fd}`); } catch { continue; }
      if (target !== "/dev/null" && !target.startsWith("/tmp/lab-logs/")) stdio.push(`${pid}:${fd}=${target}`);
    }
  }
  r.reachable_stdio = stdio.join(",") || "none";
  r.write_harness = attempt(() => { fs.appendFileSync("/opt/tblab/lab.py", "#"); });
  r.write_root = attempt(() => { fs.writeFileSync("/selftest", "x"); });
  r.uid = process.getuid();
  const st = fs.readFileSync("/proc/self/status", "utf8");
  r.cap_eff = (st.match(/^CapEff:\s*(\S+)/m) || [])[1];
  r.no_new_privs = (st.match(/^NoNewPrivs:\s*(\S+)/m) || [])[1];
  r.interfaces = Object.keys(os.networkInterfaces()).sort().join(",");
  const s = net.connect({ host: "1.1.1.1", port: 53, timeout: 3000 });
  s.on("connect", () => { r.internet = "allowed"; s.destroy(); done(r); });
  s.on("error", (e) => { r.internet = "denied:" + e.code; done(r); });
  s.on("timeout", () => { r.internet = "denied:timeout"; s.destroy(); done(r); });
}

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.id === undefined) return;
  const send = (obj) => process.stdout.write(JSON.stringify(Object.assign({ jsonrpc: "2.0", id: msg.id }, obj)) + "\n");
  if (msg.method === "initialize") {
    send({ result: { protocolVersion: "2025-06-18", capabilities: { tools: {} }, serverInfo: { name: "lab-selftest-stand-in", version: "0" } } });
  } else if (msg.method === "tools/list") {
    send({ result: { tools: [{ name: "selftest", description: "Lab isolation probe", inputSchema: { type: "object", properties: {} } }] } });
  } else if (msg.method === "tools/call") {
    probes((r) => send({ result: { content: [{ type: "text",
      text: ESC + "[2J" + ESC + "]0;title\u0007\u202e\u0085 " + JSON.stringify(r) }] } }));
  } else {
    send({ error: { code: -32601, message: "method not found" } });
  }
});
