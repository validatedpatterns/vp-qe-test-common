import logging
import re
import subprocess
import time

from itertools import chain
from ocp_resources.namespace import Namespace
from ocp_resources.pipeline import Pipeline
from ocp_resources.pipeline_run import PipelineRun
from ocp_resources.task_run import TaskRun
from ocp_resources.pod import Pod
from openshift.dynamic import DynamicClient
from openshift.dynamic.exceptions import NotFoundError

from validatedpatterns_tests.interop import application
from validatedpatterns_tests.interop.crd import ManagedCluster
from validatedpatterns_tests.interop.edge_util import (
    get_long_live_bearer_token,
    get_site_response,
)

from . import __loggername__

logger = logging.getLogger(__loggername__)


def get_missing_projects(
    openshift_dyn_client: DynamicClient, projects: list[str]
) -> list[str]:
    """
    Return missing projects from project list

    """
    missing_projects = []

    for project in projects:
        # Check for missing project
        try:
            namespaces = Namespace.get(dyn_client=openshift_dyn_client, name=project)
            next(namespaces)
        except NotFoundError:
            missing_projects.append(project)
            continue

    return missing_projects


def is_project_empty(openshift_dyn_client: DynamicClient, project: str) -> bool:
    """
    Check for absence of pods in a project
    """
    pods = Pod.get(dyn_client=openshift_dyn_client, namespace=project)

    first_pod = next(pods, None)
    if first_pod is None:
        return True
    return False


def assert_pod_status(
    openshift_dyn_client: DynamicClient, projects: list[str], skip_check: list[str] = []
):
    missing_projects = get_missing_projects(openshift_dyn_client, projects)
    empty_projects = []
    failed_pods = set()

    for project in projects:
        if is_project_empty(openshift_dyn_client, project=project):
            empty_projects.append(project)
            continue

        pods = Pod.get(dyn_client=openshift_dyn_client, namespace=project)

        for pod in pods:
            if any(name in pod.instance.metadata.name for name in skip_check):
                continue
            # Check if any of the containers are waiting (incl. CrashLoopBackOff, ImagePullBackOff) or completed with errored
            if any(
                (
                    container.state.waiting
                    or (
                        container.state.terminated
                        and container.state.terminated.reason != "Completed"
                    )
                )
                for container in chain(
                    pod.instance.status.containerStatuses or [],
                    pod.instance.status.initContainerStatuses or [],
                )
            ):
                failed_pods.add(
                    f"{pod.instance.metadata.namespace}/{pod.instance.metadata.name}"
                )

    errors = []

    if missing_projects:
        errors.append(
            f"The following namespaces are missing: {', '.join(missing_projects)}"
        )

    if empty_projects:
        errors.append(
            f"The following namespaces have no pods deployed: {', '.join(empty_projects)}"
        )

    if failed_pods:
        errors.append(
            f"The following pods are failed (ns/podname): {', '.join(failed_pods)}"
        )

    assert not errors, "\n".join(errors)


def assert_site_reachable(openshift_dyn_client: DynamicClient):
    namespace = "vp-gitops"
    sub_string = "argocd-dex-server-token"

    api_url = application.get_site_api_url(openshift_dyn_client)

    bearer_token = get_long_live_bearer_token(
        openshift_dyn_client=openshift_dyn_client,
        namespace=namespace,
        sub_string=sub_string,
    )

    api_response = get_site_response(site_url=api_url, bearer_token=bearer_token)

    assert api_response.status_code == 200, (
        f"Site is not reachable (HTTP {api_response.status_code}). URL: {api_url}"
    )


def assert_argocd_reachable(openshift_dyn_client: DynamicClient):
    namespace = "vp-gitops"
    name = "vp-gitops-server"
    sub_string = "argocd-dex-server-token"

    argocd_route_url = application.get_route_url(
        openshift_dyn_client, namespace, name
    )
    bearer_token = get_long_live_bearer_token(
        openshift_dyn_client=openshift_dyn_client,
        namespace=namespace,
        sub_string=sub_string,
    )

    argocd_route_response = get_site_response(
        site_url=argocd_route_url, bearer_token=bearer_token
    )

    assert argocd_route_response.status_code == 200, (
        f"Argocd is not reachable. Please check the deployment. (HTTP {argocd_route_response.status_code}). "
        f"URL: {argocd_route_url}"
    )


def assert_managed_clusters(
    openshift_dyn_client: DynamicClient, managed_cluster_clustergroups: list[str]
):
    not_joined_clusters = []
    for clustergroup in managed_cluster_clustergroups:
        clusters = ManagedCluster.get(
            dyn_client=openshift_dyn_client,
            label_selector=f"clusterGroup={clustergroup}",
        )

        for cluster in clusters:
            is_managed_cluster_joined, managed_cluster_status = cluster.self_registered

            if not is_managed_cluster_joined:
                not_joined_clusters.append(
                    f"{cluster.name} is not self registered, status: {managed_cluster_status}"
                )

    assert not not_joined_clusters, (
        "The following managed clusters are not self registered:\n"
        + "\n".join(not_joined_clusters)
)


def validate_pipelineruns(
    openshift_dyn_client, project, expected_pipelines, expected_pipelineruns
):
    found_pipelines = []
    found_pipelineruns = []
    passed_pipelineruns = []
    failed_pipelineruns = []

    # FAIL here if no pipelines are found
    try:
        pipelines = Pipeline.get(dyn_client=openshift_dyn_client, namespace=project)
        next(pipelines)
    except StopIteration:
        err_msg = "No pipelines were found"
        return False, err_msg

    for pipeline in Pipeline.get(dyn_client=openshift_dyn_client, namespace=project):
        for expected_pipeline in expected_pipelines:
            match = expected_pipeline + "$"
            if re.match(match, pipeline.instance.metadata.name):
                if pipeline.instance.metadata.name not in found_pipelines:
                    logger.info(f"found pipeline: {pipeline.instance.metadata.name}")
                    found_pipelines.append(pipeline.instance.metadata.name)
                    break

    if len(expected_pipelines) == len(found_pipelines):
        logger.info("Found all expected pipelines")
    else:
        err_msg = f"Some or all pipelines are missing:\nExpected: {expected_pipelines}\nFound: {found_pipelines}"
        return False, err_msg

    logger.info("Checking Openshift pipeline runs")
    timeout = time.time() + 3600

    # FAIL here if no pipelineruns are found
    try:
        pipelineruns = PipelineRun.get(
            dyn_client=openshift_dyn_client, namespace=project
        )
        next(pipelineruns)
    except StopIteration:
        err_msg = "No pipeline runs were found"
        return False, err_msg

    while time.time() < timeout:
        for pipelinerun in PipelineRun.get(
            dyn_client=openshift_dyn_client, namespace=project
        ):
            for expected_pipelinerun in expected_pipelineruns:
                if re.search(expected_pipelinerun, pipelinerun.instance.metadata.name):
                    if pipelinerun.instance.metadata.name not in found_pipelineruns:
                        logger.info(
                            f"found pipelinerun: {pipelinerun.instance.metadata.name}"
                        )
                        found_pipelineruns.append(pipelinerun.instance.metadata.name)
                        break

        if len(expected_pipelineruns) == len(found_pipelineruns):
            break
        else:
            time.sleep(60)
            continue

    if len(expected_pipelineruns) == len(found_pipelineruns):
        logger.info("Found all expected pipeline runs")
    else:
        err_msg = f"Some pipeline runs are missing:\nExpected: {expected_pipelineruns}\nFound: {found_pipelineruns}"
        return False, err_msg

    logger.info("Checking Openshift pipeline run status")
    timeout = time.time() + 3600

    while time.time() < timeout:
        for pipelinerun in PipelineRun.get(
            dyn_client=openshift_dyn_client, namespace=project
        ):
            if pipelinerun.instance.status.conditions[0].reason == "Succeeded":
                if pipelinerun.instance.metadata.name not in passed_pipelineruns:
                    logger.info(
                        f"Pipeline run succeeded: {pipelinerun.instance.metadata.name}"
                    )
                    passed_pipelineruns.append(pipelinerun.instance.metadata.name)
            elif pipelinerun.instance.status.conditions[0].reason == "Running":
                logger.info(
                    f"Pipeline {pipelinerun.instance.metadata.name} is still running"
                )
            else:
                reason = pipelinerun.instance.status.conditions[0].reason
                logger.info(
                    f"Pipeline run FAILED: {pipelinerun.instance.metadata.name} Reason: {reason}"
                )
                if pipelinerun.instance.metadata.name not in failed_pipelineruns:
                    failed_pipelineruns.append(pipelinerun.instance.metadata.name)

        logger.info(f"Failed pipelineruns: {failed_pipelineruns}")
        logger.info(f"Passed pipelineruns: {passed_pipelineruns}")

        if (len(failed_pipelineruns) + len(passed_pipelineruns)) == len(
            expected_pipelineruns
        ):
            break
        else:
            time.sleep(60)
            continue

    if ((len(failed_pipelineruns)) > 0) or (
        len(passed_pipelineruns) < len(expected_pipelineruns)
    ):
        logger.info("Checking Openshift task runs")

        # FAIL here if no task runs are found
        try:
            taskruns = TaskRun.get(dyn_client=openshift_dyn_client, namespace=project)
            next(taskruns)
        except StopIteration:
            err_msg = "No task runs were found"
            logger.error(f"FAIL: {err_msg}")
            assert False, err_msg

        for taskrun in TaskRun.get(dyn_client=openshift_dyn_client, namespace=project):
            if taskrun.instance.status.conditions[0].status == "False":
                reason = taskrun.instance.status.conditions[0].reason
                logger.info(
                    f"Task FAILED: {taskrun.instance.metadata.name} Reason: {reason}"
                )

                message = taskrun.instance.status.conditions[0].message
                logger.info(f"message: {message}")

                try:
                    cmdstring = re.search("for logs run: kubectl(.*)$", message).group(
                        1
                    )
                    cmd = str("oc" + cmdstring)
                    logger.info(f"CMD: {cmd}")
                    cmd_out = subprocess.run(cmd, shell=True, capture_output=True)

                    logger.info(cmd_out.stdout.decode("utf-8"))
                    logger.info(cmd_out.stderr.decode("utf-8"))
                except AttributeError:
                    logger.error("No logs to collect")

        err_msg = "Some or all tasks have failed"
        return False, err_msg

    else:
        return None
