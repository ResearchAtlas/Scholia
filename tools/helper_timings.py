"""The local model helper's timings on this Mac (ticket 70's measurement rules; slice 1 section 13's
calibration rule), for S1-16.

    sandbox-exec -f tests/loopback-only.sb uv run --no-sync python tools/helper_timings.py \\
        --helper <app>/Contents/MacOS/llama-server --model <Qwen3-Embedding-0.6B-Q8_0.gguf> \\
        [--reranker <qwen3-reranker-0.6b-q8_0.gguf>] [--sustained SECONDS] [--desktop] [--out FILE]

The workload is named here, before anything runs (WORKLOAD), and printed with the results: synthetic
English and Chinese passages shaped like section 7.1's (one paragraph of up to 2,000 characters,
prefixed with its title and section path), in batches of 32, and short questions, all generated from
a fixed seed; no tier 1 or other real data. The backend runs in this process on a new temporary data
folder, with an in-memory credential store (never the Keychain); the model is imported through the
API, verified like any import, and the helper the app starts is driven through the outbound gate,
as search will drive it. The helper binary must sit in an app bundle with the build's SHA-256
manifest (tools/build_app.sh), which the app checks before each launch. Run it under the loopback-only
sandbox so nothing leaves the Mac; it also lists the helpers' sockets with lsof.

Recorded: the first start in this process (the model file not in the file cache: it is installed
with caching off), warm starts, embedding latency per batch and per question (warm median and p95),
a sustained run with questions during indexing, the share of questions over the hybrid retrieval
deadline, failures, peak memory (physical footprint and resident size, sampled every 0.5 s), memory
pressure and swap. With --reranker, ticket 70's cancellation test: 24 candidates reranked against a
deadline, the reranker's own process ended at the deadline, and the time until it is gone, with
embedding work running beside it.

Without --desktop the backend runs in this process, and memory covers it and its helper processes,
from their launch. With --desktop the whole application runs as the desktop entry runs it
(backend/desktop.py: the backend, its window and the interface in it), on a new temporary data
folder, and the same workload is driven inside its backend; memory then also covers the window's
WebKit processes (found as the WebKit processes this launch started, which the system attributes
to the same responsible process), and every one of the app's processes is checked for sockets
that are not loopback.
"""

import argparse
import asyncio
import ctypes
import fcntl
import json
import os
import random
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx  # noqa: E402

from backend import local_helper  # noqa: E402
from backend.app import create_app  # noqa: E402
from backend.local_helper import EMBEDDING, RERANKER_MODEL, HelperUnavailable  # noqa: E402
from backend.self_test import _Keys  # noqa: E402

ORIGIN = "http://127.0.0.1:1"
SEED = 20261008
WORKLOAD = {
    "name": "S1-16 synthetic passages and questions, seed 20261008",
    "passages": "one paragraph each, up to 2,000 characters, prefixed with its title and section path "
                "(section 7.1); lengths drawn 30% from 200-600, 40% from 600-1,200, 20% from 1,200-1,800 "
                "and 10% from 1,800-2,000 characters, cut at a sentence end",
    "batches": "32 passages each (section 13); 20 English and 20 Chinese batches warm, after 2 warm-up batches",
    "questions": "50 English and 50 Chinese questions of 8 to 25 words (or 12 to 40 characters), one embedding each",
    "sustained": "indexing batches back to back, English and Chinese in turn, with a question 0.25 s after each answer",
    "reranking": "24 candidates per query from the same passages, 500 ms deadline; cancellation trials with a "
                 "deadline shorter than the work, so work is under way when it passes",
    "deadline_ms": 500,  # section 13's hybrid retrieval deadline, the bound a question's embedding counts toward
    "machine": "the reference Mac: Apple M5, 24 GB, macOS 27.0.1 (ticket 70)",
}

EN_WORDS = {
    "topic": ["survey response", "classroom observation", "household income", "teacher feedback", "civic participation",
              "migration history", "peer review", "reading comprehension", "public health messaging", "labour mobility"],
    "method": ["cross-sectional", "longitudinal", "mixed-methods", "quasi-experimental", "ethnographic",
               "panel", "case study", "randomized", "comparative", "interview-based"],
    "group": ["students", "households", "teachers", "respondents", "clinics", "municipalities", "workers", "schools"],
    "verb": ["measured", "coded", "estimated", "compared", "weighted", "observed", "triangulated", "modelled"],
    "result": ["a modest positive association", "no reliable difference", "a strong seasonal pattern",
               "heterogeneous effects across regions", "a decline after the policy change", "an interaction with age"],
}
ZH_WORDS = {
    "topic": ["问卷回答", "课堂观察", "家庭收入", "教师反馈", "公民参与", "迁移经历", "同行评议", "阅读理解", "公共卫生宣传", "劳动力流动"],
    "method": ["横断面", "纵向追踪", "混合方法", "准实验", "民族志", "面板数据", "个案研究", "随机对照", "比较研究", "访谈"],
    "group": ["学生", "家庭", "教师", "受访者", "诊所", "城市", "工人", "学校"],
    "verb": ["测量", "编码", "估计", "比较", "加权", "观察", "交叉验证", "建模"],
    "result": ["呈现出适度的正相关", "没有可靠的差异", "表现出明显的季节规律", "在不同地区效果不一", "在政策调整后有所下降", "与年龄存在交互作用"],
}
EN_SENTENCES = [
    "The {method} design {verb} {topic} among {n} {group} over {k} waves.",
    "Analyses of {topic} showed {result}, which held after controlling for prior attainment.",
    "We {verb} {topic} with a validated instrument and report reliability for each subscale.",
    "Missing data on {topic} were handled with multiple imputation across {k} imputed sets.",
    "Among the {n} {group}, the {method} comparison found {result}.",
    "Coders {verb} each transcript twice, and disagreements about {topic} were resolved in discussion.",
    "Sensitivity checks that excluded {group} recruited late left the estimate for {topic} unchanged.",
]
ZH_SENTENCES = [
    "本研究采用{method}设计，对{n}名{group}的{topic}进行了{k}轮{verb}。",
    "关于{topic}的分析显示，结果{result}，在控制先前成绩后依然成立。",
    "我们使用经过验证的量表{verb}了{topic}，并报告了各分量表的信度。",
    "{topic}的缺失数据采用多重插补处理，共生成{k}个插补数据集。",
    "在{n}名{group}中，{method}比较发现{topic}{result}。",
    "两位编码员对每份访谈记录各{verb}一次，关于{topic}的分歧经讨论解决。",
    "排除较晚招募的{group}后，{topic}的估计值没有变化。",
]
TITLES_EN = ["Schooling and Mobility in Three Cities", "Measuring Trust in Local Government",
             "Household Survey Methods Revisited", "Classroom Talk and Reading Growth"]
TITLES_ZH = ["三座城市的教育与流动", "地方政府信任的测量", "家庭调查方法再探", "课堂对话与阅读发展"]
SECTIONS_EN = ["Methods > Participants", "Methods > Measures", "Results > Main effects", "Discussion > Limitations"]
SECTIONS_ZH = ["方法 > 研究对象", "方法 > 测量工具", "结果 > 主要效应", "讨论 > 局限"]


def _length(rng):
    band = rng.choices([(200, 600), (600, 1200), (1200, 1800), (1800, 2000)], weights=[30, 40, 20, 10])[0]
    return rng.randint(*band)


def _paragraph(rng, words, sentences, target, joiner):
    text = ""
    while True:
        sentence = rng.choice(sentences).format(n=rng.randint(40, 4000), k=rng.randint(2, 9),
                                                **{key: rng.choice(values) for key, values in words.items()})
        if text and len(text) + len(joiner) + len(sentence) > target:
            return text
        text = f"{text}{joiner}{sentence}" if text else sentence


def passages(language, count, rng):
    """count passages: (index text, paragraph length in characters)."""
    english = language == "en"
    out = []
    for _ in range(count):
        paragraph = _paragraph(rng, EN_WORDS if english else ZH_WORDS, EN_SENTENCES if english else ZH_SENTENCES,
                               _length(rng), " " if english else "")
        title = rng.choice(TITLES_EN if english else TITLES_ZH)
        section = rng.choice(SECTIONS_EN if english else SECTIONS_ZH)
        out.append((f"{title} › {section}\n{paragraph}", len(paragraph)))
    return out


def questions(language, count, rng):
    if language == "en":
        forms = ["How was {topic} {verb} in the {method} study of {group}?",
                 "What did the {method} analysis find about {topic} among {group}?",
                 "Which {group} were excluded when {topic} was {verb}, and why?"]
        words = EN_WORDS
    else:
        forms = ["{method}研究中如何{verb}{group}的{topic}？", "关于{group}的{topic}，{method}分析发现了什么？",
                 "在{verb}{topic}时排除了哪些{group}，为什么？"]
        words = ZH_WORDS
    return [rng.choice(forms).format(**{key: rng.choice(values) for key, values in words.items()})
            for _ in range(count)]


def summary(values):
    values = sorted(values)
    if not values:
        return None
    return {"n": len(values), "median": round(statistics.median(values), 4),
            "p95": round(values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))], 4),
            "min": round(values[0], 4), "max": round(values[-1], 4)}


_libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
# proc_pid_rusage's rusage_info_v4, read as 64-bit fields after its 16-byte UUID.
_PHYS_FOOTPRINT, _LIFETIME_MAX_FOOTPRINT, _RESIDENT = 7, 28, 6


def usage(pid):
    """A process's (resident size, physical footprint, lifetime peak footprint) in bytes, or None; the
    footprint is what Activity Monitor calls Memory, Metal buffers included."""
    info = (ctypes.c_uint64 * 64)()
    if _libproc.proc_pid_rusage(ctypes.c_int(pid), ctypes.c_int(4), ctypes.byref(info)) != 0:
        return None
    fields = info[2:]  # after the UUID's 16 bytes
    return fields[_RESIDENT], fields[_PHYS_FOOTPRINT], fields[_LIFETIME_MAX_FOOTPRINT]


def footprint(pid):
    found = usage(pid)
    return found[1] if found else None


_libproc.proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
_libproc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
_libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
_responsible = ctypes.CDLL("/usr/lib/libSystem.B.dylib").responsibility_get_pid_responsible_for_pid
_responsible.argtypes, _responsible.restype = [ctypes.c_int], ctypes.c_int


_libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
_libsystem.sandbox_check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]


def sandboxed() -> bool:
    """Whether this process runs under a sandbox profile (sandbox-exec), by sandbox_check."""
    return _libsystem.sandbox_check(os.getpid(), None, 0) == 1


def network_isolation() -> str:
    """What kept a desktop run's network on this Mac, for its results."""
    snapshot = ("not_loopback_sockets is a snapshot of every process of the app, WebKit's included, at the end of "
                "the run.")
    if not sandboxed():
        return "Not run under a sandbox profile: nothing confined the app's network. " + snapshot
    return ("A sandbox profile (tests/loopback-only.sb, as the docstring says) confines this Python process and its "
            "children (the backend and its helpers) only. The window's WebKit processes are the system's XPC "
            "services, outside it: they reach what the page asks for, which loads only its loopback URL. " + snapshot)


def _pids(listing, *args):
    buffer = (ctypes.c_int * 8192)()
    count = listing(*args, buffer, ctypes.sizeof(buffer))
    return [pid for pid in buffer[:max(count, 0)] if pid > 0]


def path_of(pid) -> str:
    buffer = ctypes.create_string_buffer(4096)
    return buffer.value.decode(errors="replace") if _libproc.proc_pidpath(pid, buffer, 4096) > 0 else ""


def helpers_of(pid) -> list[int]:
    """The llama-server processes this process started, from their launch (not only once ready)."""
    return [child for child in _pids(_libproc.proc_listchildpids, pid) if path_of(child).endswith("/llama-server")]


def webkit_processes() -> set[int]:
    return {pid for pid in _pids(_libproc.proc_listallpids) if "/WebKit.framework/" in path_of(pid)}


def webkit_of(pid, before: set[int]) -> list[int]:
    """The WebKit processes started since `before` was taken and attributed to the same responsible
    process as this one: the window's."""
    ours = {pid, _responsible(pid)}
    return sorted(other for other in webkit_processes() - before if _responsible(other) in ours)


class Memory:
    """Samples the memory of the processes pids() lists every 0.5 s, and the system's memory
    pressure level and free percentage every 2 s."""

    def __init__(self, pids):
        self.pids, self.samples, self.pressure, self.stop = pids, [], [], threading.Event()
        self.names = {}  # each process's executable name, read while it runs
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        tick = 0
        while not self.stop.wait(0.5):
            found = {pid: usage(pid) for pid in self.pids() if pid}
            for pid in found:
                self.names.setdefault(pid, Path(path_of(pid)).name)
            self.samples.append({pid: value for pid, value in found.items() if value})
            if tick % 4 == 0:
                level = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                                       capture_output=True, text=True).stdout.strip()
                free = re.search(r"free percentage: (\d+)%", subprocess.run(
                    ["memory_pressure", "-Q"], capture_output=True, text=True).stdout)
                self.pressure.append((int(level), int(free.group(1)) if free else None))
            tick += 1

    def peak(self):
        footprints = [sum(value[1] for value in sample.values()) for sample in self.samples]
        resident = [sum(value[0] for value in sample.values()) for sample in self.samples]
        top = self.samples[footprints.index(max(footprints))] if footprints else {}
        return {"footprint_peak_bytes": max(footprints, default=None),
                "resident_peak_bytes": max(resident, default=None), "samples": len(footprints),
                # the peak sample, process by process (by executable name)
                "footprint_peak_by_process": {f"{self.names.get(pid, '')} {pid}": value[1]
                                              for pid, value in top.items()},
                "pressure_level_max": max((level for level, _ in self.pressure), default=None),
                "pressure_samples": len(self.pressure),
                "free_percent_min": min((free for _, free in self.pressure if free is not None), default=None)}


def vm_counters():
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    counters = {key: int(value) for key, value in re.findall(r"^(Swapins|Swapouts|Pageouts):\s+(\d+)\.", out, re.M)}
    counters["swap_used"] = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout.strip()
    return counters


def sockets(pid):
    """The helper's sockets as lsof lists them: every one must be on 127.0.0.1."""
    out = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid), "-i"], capture_output=True, text=True).stdout
    return [line.split(None, 8)[-1] for line in out.splitlines()[1:]]


def not_loopback(pids) -> list[str]:
    """Any socket of these processes with an end that is not loopback."""
    loopback = re.compile(r"^(127\.0\.0\.1|\[::1\]|localhost|\*)(:|$)")
    return [f"{pid} {name}" for pid in pids for name in sockets(pid)
            if not all(loopback.match(end) for end in name.split(" ")[0].split("->"))]


def uncached_copy(source: Path, target: Path):
    """Copy a file with the file cache off for the copy, so the next read of it comes from disk."""
    with open(source, "rb") as src, open(target, "wb") as out:
        fcntl.fcntl(src.fileno(), fcntl.F_NOCACHE, 1)
        fcntl.fcntl(out.fileno(), fcntl.F_NOCACHE, 1)
        while block := src.read(8 << 20):
            out.write(block)


async def timed(fn):
    began = time.perf_counter()
    result = await fn
    return time.perf_counter() - began, result


async def run(args):
    """The workload with the backend in this process, on a temporary data folder removed at the end."""
    with tempfile.TemporaryDirectory(prefix="scholia-timings-") as folder:
        data = Path(folder)
        app = create_app(data, origin=ORIGIN, keyring_backend=_Keys(), helper=local_helper.Config(binary=Path(args.helper)))
        async with app.app.router.lifespan_context(app.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
                                         headers={"X-Scholia-Client": "local"}, timeout=600) as client:
                results = await measure(args, app.app.state.scholia, client, data,
                                        lambda: [os.getpid(), *helpers_of(os.getpid())])
    results["memory"]["covers"] = "this backend process and its helper processes, from their launch"
    return results


def run_desktop(args, workload=None):
    """The workload inside the desktop application: its backend, its window and the interface,
    started as backend/desktop.py starts them, on a new temporary data folder with an in-memory
    credential store. The window closes when the workload ends, and the data folder is removed.
    workload: a measure function of the same form as this module's (S1-17's search timings pass theirs)."""
    with tempfile.TemporaryDirectory(prefix="scholia-timings-desktop-") as folder:
        return _run_desktop(args, Path(folder), workload or measure)


def _run_desktop(args, data, measured):
    import webview

    import backend.app
    from backend import desktop

    found, before = {}, webkit_processes()
    create_app_, run_server = backend.app.create_app, desktop._run_server

    def create(*args_, **options):  # the desktop entry's app, with this helper binary (none from source)
        found["app"] = create_app_(*args_, helper=local_helper.Config(binary=Path(args.helper)), **options)
        return found["app"]

    def serve(server, sock, loop):
        found["loop"] = loop  # filled with the server's event loop once it runs
        run_server(server, sock, loop)

    def drive(url):
        try:
            origin, session = url.split("/#session=")
            deadline = time.monotonic() + 30
            while not any("WebContent" in path_of(pid) for pid in webkit_of(os.getpid(), before)):
                if time.monotonic() > deadline:
                    raise RuntimeError("the window's WebKit processes were not found")
                time.sleep(0.2)
            time.sleep(3)  # the interface loaded and settled

            async def workload():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=found["app"]), base_url=origin,
                                             headers={"X-Scholia-Client": "local", "X-Scholia-Session": session},
                                             timeout=600) as client:
                    return await measured(args, found["app"].app.state.scholia, client, data, processes)

            found["results"] = asyncio.run_coroutine_threadsafe(workload(), found["loop"]["loop"]).result()
        except BaseException as error:  # reported once the window has closed
            found["error"] = error
        finally:
            for window in list(webview.windows):
                window.destroy()

    def processes():
        found["webkit"] = webkit_of(os.getpid(), before)
        return [os.getpid(), *helpers_of(os.getpid()), *found["webkit"]]

    def open_window(url):
        threading.Thread(target=drive, args=(url,), daemon=True).start()
        desktop._webview_window(url)

    backend.app.create_app, desktop._run_server = create, serve
    try:
        code = desktop.run(data, open_window, keyring_backend=_Keys())
    finally:
        backend.app.create_app, desktop._run_server = create_app_, run_server
    if "error" in found:
        raise found["error"]
    results = found["results"]
    results["memory"]["covers"] = ("the desktop application: its process (backend, window and interface), its helper "
                                   "processes from their launch, and its window's WebKit processes")
    results["memory"]["webkit_processes"] = [Path(path_of(pid)).name for pid in found.get("webkit", [])]
    results["network_isolation"] = network_isolation()
    results["desktop_exit_code"] = code
    return results


async def measure(args, state, client, data, processes):
    """The workload, through the app whose state and API client are given; processes() lists the
    process ids memory covers."""
    rng = random.Random(SEED)
    english = passages("en", 32 * 22, rng)
    chinese = passages("zh", 32 * 22, rng)
    asks = [("en", q) for q in questions("en", 50, rng)] + [("zh", q) for q in questions("zh", 50, rng)]
    results = {"workload": WORKLOAD, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    results["passage_chars"] = {lang: summary([n for _, n in items]) for lang, items in (("en", english), ("zh", chinese))}
    seconds, response = await timed(client.post("/api/helper/models/import",
                                                json={"model": EMBEDDING, "path": str(Path(args.model).resolve())}))
    assert response.status_code == 200, response.text
    results["import_seconds"] = round(seconds, 2)
    installed = data / "models" / EMBEDDING / local_helper.EMBEDDING_MODEL["file"]
    uncached_copy(Path(args.model), installed)  # the first start reads it from disk, as after a restart
    local = state["local_helper"]
    helper = local.helpers[EMBEDDING]
    memory = Memory(processes)
    vm_before = vm_counters()
    memory.thread.start()

    # Starts: the first (cold file cache), then warm ones.
    seconds, _ = await timed(local_helper.embed(state, [asks[0][1]], query=True))
    results["first_start"] = {"ready_seconds": helper.ready_seconds, "first_question_seconds": round(seconds, 3)}
    warm = []
    for _ in range(10):
        await helper.stop()
        seconds, _ = await timed(local_helper.embed(state, [asks[1][1]], query=True))
        warm.append((helper.ready_seconds, seconds))
    results["warm_start_ready_seconds"] = summary([r for r, _ in warm])
    results["warm_start_first_question_seconds"] = summary([s for _, s in warm])
    results["helper_sockets"] = sockets(helper._process.pid)

    # Tokens per passage, as the helper counts them (each must fit one slot's 2,048).
    tokens = {}
    for lang, items in (("en", english), ("zh", chinese)):
        counts = []
        for text, _ in items[:64]:
            reply = await helper._post("/tokenize", {"content": text}, None)
            counts.append(len(reply["tokens"]))
        tokens[lang] = summary(counts)
    results["passage_tokens_sample_of_64"] = tokens

    # Warm batches and questions.
    batch_times, failures = {"en": [], "zh": []}, []
    for lang, items in (("en", english), ("zh", chinese)):
        for i in range(22):
            texts = [text for text, _ in items[i * 32:(i + 1) * 32]]
            try:
                seconds, vectors = await timed(local_helper.embed(state, texts))
                assert len(vectors) == 32 and len(vectors[0]) == 1024
                if i >= 2:
                    batch_times[lang].append(seconds)
            except HelperUnavailable as error:
                failures.append(("batch", lang, i, error.reason))
    results["batch_seconds"] = {lang: summary(values) for lang, values in batch_times.items()}
    question_times = {"en": [], "zh": []}
    for lang, question in asks:
        seconds, _ = await timed(local_helper.embed(state, [question], query=True))
        question_times[lang].append(seconds)
    results["question_seconds"] = {lang: summary(values) for lang, values in question_times.items()}
    results["footprint_after_warm_bytes"] = {"backend": footprint(os.getpid()),
                                             "helper": footprint(helper._process.pid)}

    # Sustained: indexing back to back, a question 0.25 s after each answer.
    sustained_q, sustained_b = [], []

    async def batch(i):
        items = english if i % 2 == 0 else chinese
        start = (i // 2 % 22) * 32
        try:
            seconds, _ = await timed(local_helper.embed(state, [t for t, _ in items[start:start + 32]]))
            sustained_b.append(seconds)
        except HelperUnavailable as error:
            failures.append(("sustained batch", i, error.reason))

    async def ask(i):
        try:
            seconds, _ = await timed(local_helper.embed(state, [asks[i % len(asks)][1]], query=True))
            sustained_q.append(seconds)
        except HelperUnavailable as error:
            failures.append(("sustained question", i, error.reason))
        await asyncio.sleep(0.25)

    began = time.monotonic()
    ended, _ = await asyncio.gather(back_to_back(batch, began + args.sustained),
                                    back_to_back(ask, began + args.sustained))
    deadline = WORKLOAD["deadline_ms"] / 1000
    results["sustained"] = {
        "seconds": args.sustained, "batches": summary(sustained_b), "questions": summary(sustained_q),
        "questions_over_deadline": sum(1 for s in sustained_q if s > deadline),
        "questions_over_1s": sum(1 for s in sustained_q if s > 1),
        # over the time indexing took: from the start to the end of its last batch, past `seconds`
        "indexing_elapsed_seconds": round(ended - began, 3),
        "passages_per_second": throughput(32 * len(sustained_b), ended - began),
    }
    results["footprint_after_sustained_bytes"] = {"backend": footprint(os.getpid()),
                                                  "helper": footprint(helper._process.pid)}
    results["helper_lifetime_peak_footprint_bytes"] = usage(helper._process.pid)[2]
    results["helper_sockets_after_sustained"] = sockets(helper._process.pid)

    if args.reranker:
        results["reranker"] = await reranking(state, local, Path(args.reranker), english, chinese, asks)
    results["failures"] = failures
    memory.stop.set()
    memory.thread.join()
    results["memory"] = memory.peak()
    results["not_loopback_sockets"] = not_loopback(processes())  # every process of the app, at the end
    results["vm_before"], results["vm_after"] = vm_before, vm_counters()
    results["audit_rows"] = await asyncio.to_thread(state["db"].read, lambda conn: conn.execute(
        "SELECT data ->> 'kind', data ->> 'decision', count(*) FROM audit_log WHERE event = 'outbound'"
        " GROUP BY 1, 2").fetchall())
    return results


async def back_to_back(work, until, clock=time.monotonic):
    """Runs work(0), work(1), ... one after another while clock() is before `until`; each one started
    runs to its end. Returns the clock when the last one ended, which can be past `until`."""
    i, ended = 0, clock()
    while clock() < until:
        await work(i)
        i, ended = i + 1, clock()
    return ended


def throughput(passages, seconds):
    """Passages per second over the time the work took."""
    return round(passages / seconds, 1) if seconds > 0 else None


async def reranking(state, local, path, english, chinese, asks):
    reranker = local.add(RERANKER_MODEL, path)
    out = {}
    rng = random.Random(SEED + 1)
    pool = english + chinese
    query = asks[0][1]
    candidates = [text for text, _ in rng.sample(pool, 24)]
    seconds, scores = await timed(reranker.rerank(query, candidates, deadline=60))
    out["first_start_ready_seconds"] = reranker.ready_seconds
    out["first_rerank_seconds"] = round(seconds, 3)
    out["sample_scores"] = [round(s, 4) for s in scores[:5]] if scores else scores
    latencies, completed = [], 0
    for _ in range(20):
        candidates = [text for text, _ in rng.sample(pool, 24)]
        seconds, scores = await timed(reranker.rerank(query, candidates, deadline=60))
        latencies.append(seconds)
    out["rerank_24_seconds"] = summary(latencies)
    # At the 500 ms deadline as specified: how many finish, and the grace when one does not.
    for deadline in (0.5, None):
        graces, restarts, finished, isolation = [], [], 0, []
        for _ in range(20):
            limit = deadline or max(0.02, statistics.median(latencies) / 4)  # work under way at the deadline
            candidates = [text for text, _ in rng.sample(pool, 24)]
            if reranker.state != "running":
                seconds, _ = await timed(reranker.rerank(query, candidates[:1], deadline=60))
                restarts.append(reranker.ready_seconds)
            pid = reranker._process.pid
            question = asyncio.create_task(timed(local_helper.embed(state, [asks[2][1]], query=True)))
            began = time.perf_counter()
            scores = await reranker.rerank(query, candidates, deadline=limit)
            returned = time.perf_counter()
            if scores is not None:
                finished += 1
            else:
                try:
                    os.kill(pid, 0)
                    gone = False
                except ProcessLookupError:
                    gone = True
                graces.append((returned - began - limit, gone))
            seconds, vectors = await question
            isolation.append(seconds)
        key = "deadline_500ms" if deadline else "deadline_shorter_than_work"
        out[key] = {"deadline_seconds": None if deadline is None else deadline, "finished_in_time": finished,
                    "cancelled": len(graces), "grace_seconds": summary([g for g, _ in graces]),
                    "process_gone_after_every_cancel": all(gone for _, gone in graces),
                    "restart_ready_seconds": summary([r for r in restarts if r is not None]),
                    "concurrent_question_seconds": summary(isolation)}
    out["embedding_helper_state"] = local.helpers[EMBEDDING].state
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--helper", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reranker")
    parser.add_argument("--sustained", type=int, default=180)
    parser.add_argument("--desktop", action="store_true", help="run the workload inside the desktop app")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps({"workload": WORKLOAD}, ensure_ascii=False, indent=2), flush=True)
    results = run_desktop(args) if args.desktop else asyncio.run(run(args))
    text = json.dumps(results, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")


if __name__ == "__main__":
    main()
