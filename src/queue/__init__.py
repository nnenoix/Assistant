"""Background queue (arq) for long-running agent jobs.

`worker.WorkerSettings` lists the registered tasks. Jobs are enqueued via
`from arq import create_pool; pool.enqueue_job('task_name', ...)`.
"""
