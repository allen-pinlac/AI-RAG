import asyncio
import logging
import threading
from typing import Any, Callable, Optional

from core.base import OrchestrationConfig, OrchestrationProvider, Workflow

logger = logging.getLogger()


class HatchetOrchestrationProvider(OrchestrationProvider):
    def __init__(self, config: OrchestrationConfig):
        super().__init__(config)
        try:
            from hatchet_sdk import ClientConfig, Hatchet
        except ImportError:
            raise ImportError(
                "Hatchet SDK not installed. Please install it using `pip install hatchet-sdk`."
            )
        logging.basicConfig(level=logging.INFO)
        root_logger = logging.getLogger()

        self.orchestrator = Hatchet(
            debug=True,
            config=ClientConfig(
                logger=root_logger,
            ),
        )
        self.root_logger = root_logger
        self.config: OrchestrationConfig = config  # for type hinting
        self.messages: dict[str, str] = {}

    def workflow(self, *args, **kwargs) -> Callable:
        return self.orchestrator.workflow(*args, **kwargs)

    def step(self, *args, **kwargs) -> Callable:
        return self.orchestrator.step(*args, **kwargs)

    def failure(self, *args, **kwargs) -> Callable:
        return self.orchestrator.on_failure_step(*args, **kwargs)

    def get_worker(self, name: str, max_runs: Optional[int] = None) -> Any:
        if not max_runs:
            max_runs = self.config.max_runs
        self.worker = self.orchestrator.worker(name, max_runs)
        return self.worker

    def concurrency(self, *args, **kwargs) -> Callable:
        return self.orchestrator.concurrency(*args, **kwargs)

    async def start_worker(self):
        if not self.worker:
            raise ValueError(
                "Worker not initialized. Call get_worker() first."
            )

        asyncio.create_task(self.worker.async_start())

    # # Instead of using asyncio.create_task, run the worker in a separate thread
    # def start_worker(self):
    #     if not self.worker:
    #         raise ValueError(
    #             "Worker not initialized. Call get_worker() first."
    #         )

    #     def run_worker():
    #         # Create a new event loop for this thread
    #         loop = asyncio.new_event_loop()
    #         asyncio.set_event_loop(loop)
    #         loop.run_until_complete(self.worker.async_start())
    #         loop.run_forever()  # If needed, or just run_until_complete for one task

    #     thread = threading.Thread(target=run_worker, daemon=True)
    #     thread.start()

    async def run_workflow(
        self,
        workflow_name: str,
        parameters: dict,
        options: dict,
        *args,
        **kwargs,
    ) -> Any:
        task_id = self.orchestrator.admin.run_workflow(
            workflow_name,
            parameters,
            options=options,
            *args,
            **kwargs,
        )
        return {
            "task_id": str(task_id),
            "message": self.messages.get(
                workflow_name, "Workflow queued successfully."
            ),  # Return message based on workflow name
        }

    def register_workflows(
        self, workflow: Workflow, service: Any, messages: dict
    ) -> None:
        self.messages.update(messages)

        logger.info(
            f"Registering workflows for {workflow} with messages {messages}."
        )
        if workflow == Workflow.INGESTION:
            from core.main.orchestration.hatchet.ingestion_workflow import (
                hatchet_ingestion_factory,
            )

            workflows = hatchet_ingestion_factory(self, service)
            if self.worker:
                for workflow in workflows.values():
                    self.worker.register_workflow(workflow)

        elif workflow == Workflow.KG:
            from core.main.orchestration.hatchet.kg_workflow import (
                hatchet_kg_factory,
            )

            workflows = hatchet_kg_factory(self, service)
            if self.worker:
                for workflow in workflows.values():
                    self.worker.register_workflow(workflow)