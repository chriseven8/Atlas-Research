import logging
import threading

from .domain import LeaseLost
from .settings import Settings
from .storage import Repository
from .workflow import ResearchWorkflow, public_error

log = logging.getLogger("research.worker")


def execute_job(repo: Repository, settings: Settings, job: dict, provider=None):
    stop = threading.Event()

    def renew():
        while not stop.wait(settings.lease_seconds / 3):
            try:
                if not repo.heartbeat(job["id"], job["owner"]):
                    return
            except Exception:
                log.warning("Heartbeat unavailable for job %s", job["id"])
                return

    heartbeat = threading.Thread(target=renew, daemon=True)
    heartbeat.start()
    try:
        result = ResearchWorkflow(settings, repo, job, provider=provider).run()
        repo.finish(job["id"], job["owner"], result["report"]["status"])
    except LeaseLost:
        log.info("Job %s no longer owned by this worker", job["id"])
    except Exception as exc:
        # Do not log raw exception strings: http clients may include credentials in URLs.
        log.error("Job %s failed: %s", job["id"], type(exc).__name__)
        try:
            repo.finish(job["id"], job["owner"], "failed", public_error(exc))
        except LeaseLost:
            pass
    finally:
        stop.set()
        heartbeat.join(timeout=2)


def run_worker(repo: Repository, settings: Settings, stop: threading.Event):
    while not stop.is_set():
        try:
            job = repo.claim()
            if job:
                execute_job(repo, settings, job)
            else:
                stop.wait(0.7)
        except Exception as exc:
            log.error("Worker loop unavailable: %s", type(exc).__name__)
            stop.wait(3)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings()
    repo = Repository(settings)
    repo.initialize()
    stop = threading.Event()
    try:
        run_worker(repo, settings, stop)
    except KeyboardInterrupt:
        stop.set()
    finally:
        repo.close()


if __name__ == "__main__":
    main()
