import os
import tempfile
from typing import Any, Callable, ClassVar, Dict, List, NamedTuple, Protocol, Sequence, Tuple

from dlt.common.configuration.container import ContainerInjectableContext
from dlt.common.configuration import configspec
from dlt.common.destination import DestinationReference
from dlt.common.schema import Schema
from dlt.common.schema.typing import TColumnSchema, TWriteDisposition


class LoadInfo(NamedTuple):
    """A tuple holding the information on recently loaded packages. Returned by pipeline run method"""
    pipeline: "SupportsPipeline"
    destination_name: str
    destination_displayable_credentials: str
    dataset_name: str
    loads_ids: Dict[str, bool]
    failed_jobs: Dict[str, Sequence[Tuple[str, str]]]

    def __str__(self) -> str:
        msg = f"{len(self.loads_ids)} load package(s) were loaded to destination {self.destination_name} and into dataset {self.dataset_name}\n"
        msg += f"The {self.destination_name} destination used {self.destination_displayable_credentials} location to store data\n"
        for load_id, completed in self.loads_ids.items():
            cstr = "COMPLETED" if completed else "NOT COMPLETED"

            # now enumerate all complete loads if we have any failed packages
            # complete but failed job will not raise any exceptions
            failed_jobs = self.failed_jobs.get(load_id)
            jobs_str = "no failed jobs\n" if not failed_jobs else f"{len(failed_jobs)} FAILED job(s)!\n"
            msg += f"Load package {load_id} is {cstr} and contains {jobs_str}"
            for job_id, failed_message in failed_jobs:
                msg += f"\t{job_id}: {failed_message}\n"
        return msg

class SupportsPipeline(Protocol):
    """A protocol with core pipeline operations that lets high level abstractions ie. sources to access pipeline methods and properties"""
    def run(
        self,
        data: Any = None,
        *,
        destination: DestinationReference = None,
        dataset_name: str = None,
        credentials: Any = None,
        table_name: str = None,
        write_disposition: TWriteDisposition = None,
        columns: Sequence[TColumnSchema] = None,
        schema: Schema = None
    ) -> LoadInfo:
        ...


@configspec(init=True)
class PipelineContext(ContainerInjectableContext):
    # TODO: declare unresolvable generic types that will be allowed by configspec
    _deferred_pipeline: Callable[[], SupportsPipeline]
    _pipeline: SupportsPipeline

    can_create_default: ClassVar[bool] = False

    def pipeline(self) -> SupportsPipeline:
        """Creates or returns exiting pipeline"""
        if not self._pipeline:
            # delayed pipeline creation
            self._pipeline = self._deferred_pipeline()
        return self._pipeline

    def activate(self, pipeline: SupportsPipeline) -> None:
        self._pipeline = pipeline

    def is_activated(self) -> bool:
        return self._pipeline is not None

    def deactivate(self) -> None:
        self._pipeline = None

    def __init__(self, deferred_pipeline: Callable[..., SupportsPipeline]) -> None:
        """Initialize the context with a function returning the Pipeline object to allow creation on first use"""
        self._deferred_pipeline = deferred_pipeline


def get_default_working_dir() -> str:
    """ Gets default working dir of the pipeline, which may be
        1. in user home directory ~/.dlt/pipelines/
        2. if current user is root in /var/dlt/pipelines
        3. if current user does not have a home directory in /tmp/dlt/pipelines
    """
    if os.geteuid() == 0:
        # we are root so use standard /var
        return os.path.join("/var", "dlt", "pipelines")

    home = _get_home_dir()
    if home is None:
        # no home dir - use temp
        return os.path.join(tempfile.gettempdir(), "dlt", "pipelines")
    else:
        # if home directory is available use ~/.dlt/pipelines
        return os.path.join(home, ".dlt", "pipelines")


def _get_home_dir() -> str:
    return os.path.expanduser("~")
