import argparse
import json
from pathlib import Path

from .domain import ResearchRequest
from .settings import Settings
from .storage import Repository
from .worker import execute_job


def main():
    parser = argparse.ArgumentParser(description="Atlas Financial Research")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="生成无需网络或密钥的演示报告")
    demo.add_argument("--symbol", default="AAPL", choices=["AAPL", "MSFT", "NVDA", "SPY"])
    demo.add_argument("--as-of", default=None)
    demo.add_argument("--output", default="data/demo-report.md")
    sub.add_parser("worker", help="运行独立研究 Worker")
    args = parser.parse_args()
    if args.command == "worker":
        from .worker import main as worker_main

        worker_main()
        return
    settings = Settings(embed_worker=False)
    repo = Repository(settings)
    repo.initialize()
    payload = {"symbol": args.symbol}
    if args.as_of:
        payload["as_of"] = args.as_of
    job_id, _ = repo.create(ResearchRequest(**payload))
    # Drain earlier queued jobs too, rather than assuming this job is at the head of the queue.
    while True:
        job = repo.claim()
        if job is None:
            break
        execute_job(repo, settings, job)
        if job["id"] == job_id:
            break
    result = repo.get(job_id)
    if not result or not result["report"]:
        raise SystemExit("报告尚未完成；可能已由另一 Worker 领取，请通过 API 查询任务。")
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result["report"]["markdown"], encoding="utf-8")
    path.with_suffix(".json").write_text(
        json.dumps(result["report"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"DEMO report: {path.resolve()}\nTask: {job_id}")
    repo.close()


if __name__ == "__main__":
    main()
