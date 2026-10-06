"""edge-tts 子进程工作器（由项目 ``.venv`` 的解释器执行）。

为什么是独立文件 + 子进程，而不是进程内 ``asyncio.run``：

1. 系统 python3 通常装不了 ``edge-tts``（PEP 668），而项目 ``.venv`` 里装了。
   用子进程调用它，主程序就能继续「零第三方依赖」。
2. 文本走 JSON 文件而不是命令行参数，长中文正文不会踩转义/长度上限的坑。
3. 每个片段单独捕获异常并写进结果文件 —— 一段失败只重试这一段，
   不必整批重来（一本书几百章，重试代价必须可控）。

用法（父进程自动调用，一般不需要手动跑）::

    ./.venv/bin/python autoread/edge_worker.py /tmp/job.json
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


async def _synthesize_one(edge_tts, voice: str, job_defaults: dict, item: dict, semaphore) -> dict:
    async with semaphore:
        output = Path(item["out_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            communicate = edge_tts.Communicate(
                item["text"],
                voice,
                rate=item.get("rate") or job_defaults.get("rate"),
                pitch=item.get("pitch") or job_defaults.get("pitch") or "+0Hz",
                volume=item.get("volume") or job_defaults.get("volume") or "+0%",
                # 显式给超时：整本书要跑几百个片段，一个卡住的连接会占住一个并发槽，
                # 默认值下要等十几秒才让位。短超时 + 上层重试，整体吞吐更稳。
                connect_timeout=job_defaults.get("connect_timeout") or 10,
                receive_timeout=job_defaults.get("receive_timeout") or 60,
            )
            await communicate.save(str(output))
        except Exception as exc:  # noqa: BLE001 - 逐条失败要回报给父进程，而不是中断整批
            return {"id": item.get("id"), "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        if not output.is_file() or output.stat().st_size < 512:
            return {"id": item.get("id"), "ok": False, "error": "输出为空（未产出有效音频）"}
        return {"id": item.get("id"), "ok": True, "error": ""}


async def _run(job: dict) -> list[dict]:
    import edge_tts  # 只有这个解释器装了它

    voice = job["voice"]
    defaults = {
        "rate": job.get("rate"),
        "pitch": job.get("pitch"),
        "volume": job.get("volume"),
    }
    concurrency = max(1, int(job.get("concurrency", 3)))
    semaphore = asyncio.Semaphore(concurrency)
    items = job.get("items") or []
    tasks = [_synthesize_one(edge_tts, voice, defaults, item, semaphore) for item in items]
    results: list[dict] = []
    done = 0
    for coroutine in asyncio.as_completed(tasks):
        result = await coroutine
        results.append(result)
        done += 1
        status = "ok" if result["ok"] else "fail"
        print(f"[edge_worker] {done}/{len(tasks)} {status}", file=sys.stderr, flush=True)
    return results


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法: python edge_worker.py <job.json>", file=sys.stderr)
        return 2
    job_path = Path(argv[1])
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"无法读取任务文件: {exc}", file=sys.stderr)
        return 2

    try:
        results = asyncio.run(_run(job))
    except ImportError as exc:
        print(f"当前解释器没有 edge-tts: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"工作器异常: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4

    result_path = job.get("result_path")
    if result_path:
        Path(result_path).write_text(
            json.dumps(results, ensure_ascii=False), encoding="utf-8"
        )
    failed = [item for item in results if not item["ok"]]
    if failed:
        print(f"[edge_worker] {len(failed)}/{len(results)} 段失败", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
