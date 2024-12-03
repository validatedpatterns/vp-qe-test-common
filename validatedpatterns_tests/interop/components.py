import logging
import os
import re
import subprocess
import time
import yaml

from ocp_resources.namespace import Namespace
from ocp_resources.pipeline import Pipeline
from ocp_resources.pipelineruns import PipelineRun
from ocp_resources.task_run import TaskRun
from ocp_resources.pod import Pod
from openshift.dynamic.exceptions import NotFoundError

from validatedpatterns_tests.interop import application
from validatedpatterns_tests.interop.crd import ManagedCluster

from . import __loggername__

logger = logging.getLogger(__loggername__)

oc = os.environ["HOME"] + "/oc_client/oc"


def dump_openshift_version():
    version_out = subprocess.run(["oc", "version"], capture_output=True)
    version_out = version_out.stdout.decode("utf-8")
    return version_out


def dump_pvc():
    pvcs_out = subprocess.run(["oc", "get", "pvc", "-A"], capture_output=True)
    pvcs_out = pvcs_out.stdout.decode("utf-8")
    return pvcs_out


def describe_pod(project, pod):
    cmd_out = subprocess.run(
        [oc, "describe", "pod", "-n", project, pod], capture_output=True
    )
    if cmd_out.stdout:
        return cmd_out.stdout.decode("utf-8")
    else:
        assert False, cmd_out.stderr


def get_log_output(project, pod, container):
    cmd_out = subprocess.run(
        [oc, "logs", "-n", project, pod, "-c", container], capture_output=True
    )
    if cmd_out.stdout:
        return cmd_out.stdout.decode("utf-8")
    else:
        assert False, cmd_out.stderr


def check_project_absence(openshift_dyn_client, projects):
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


def check_pod_absence(openshift_dyn_client, project):
    # Check for absence of pods in project
    missing_pods = []
    try:
        pods = Pod.get(dyn_client=openshift_dyn_client, namespace=project)
        next(pods)
    except StopIteration:
        missing_pods.append(project)
    return missing_pods


def check_pod_status(openshift_dyn_client, projects, skip_check=""):
    missing_projects = check_project_absence(openshift_dyn_client, projects)
    missing_pods = []
    failed_pods = []
    err_msg = []

    for project in projects:
        logger.info(f"Checking pods in namespace '{project}'")
        missing_pods += check_pod_absence(openshift_dyn_client, project)
        pods = Pod.get(dyn_client=openshift_dyn_client, namespace=project)
        for pod in pods:
            flag = ""
            if skip_check:
                for skip in skip_check:
                    if skip in pod.instance.metadata.name:
                        logger.info(f"Skipping: {pod.instance.metadata.name}")
                        flag = "skipped"
                        break

            if flag == "skipped":
                continue

            for container in pod.instance.status.containerStatuses:
                logger.info(
                    f"{pod.instance.metadata.name} : {container.name} :"
                    f" {container.state}"
                )
                if container.state.terminated:
                    if container.state.terminated.reason != "Completed":
                        logger.info(
                            f"Pod {pod.instance.metadata.name} in"
                            f" {pod.instance.metadata.namespace} namespace is"
                            " FAILED:"
                        )
                        failed_pods.append(pod.instance.metadata.name)
                        logger.info(describe_pod(project, pod.instance.metadata.name))
                        logger.info(
                            get_log_output(
                                project,
                                pod.instance.metadata.name,
                                container.name,
                            )
                        )
                elif not container.state.running:
                    logger.info(
                        f"Pod {pod.instance.metadata.name} in"
                        f" {pod.instance.metadata.namespace} namespace is"
                        " FAILED:"
                    )
                    failed_pods.append(pod.instance.metadata.name)
                    logger.info(describe_pod(project, pod.instance.metadata.name))
                    logger.info(
                        get_log_output(
                            project, pod.instance.metadata.name, container.name
                        )
                    )

    if missing_projects:
        err_msg.append(f"The following namespaces are missing: {missing_projects}")

    if missing_pods:
        err_msg.append(
            f"The following namespaces have no pods deployed: {missing_pods}"
        )

    if failed_pods:
        err_msg.append(f"The following pods are failed: {failed_pods}")

    if err_msg:
        return False, err_msg
    else:
        return None


def validate_site_reachable(kube_config, openshift_dyn_client):
    namespace = "openshift-gitops"
    sub_string = "argocd-dex-server-token"

    api_url = application.get_site_api_url(kube_config)
    api_response = application.get_site_api_response(
        openshift_dyn_client, api_url, namespace, sub_string
    )

    logger.info(f"Site API response : {api_response}")

    if api_response.status_code != 200:
        err_msg = "Site is not reachable. Please check the deployment."
        return False, err_msg
    else:
        return None


def validate_argocd_reachable(openshift_dyn_client):
    namespace = "openshift-gitops"
    name = "openshift-gitops-server"
    sub_string = "argocd-dex-server-token"
    logger.info("Check if argocd route/url on hub site is reachable")
    try:
        argocd_route_url = application.get_argocd_route_url(
            openshift_dyn_client, namespace, name
        )
        argocd_route_response = application.get_site_api_response(
            openshift_dyn_client, argocd_route_url, namespace, sub_string
        )
    except StopIteration:
        err_msg = "Argocd url/route is missing in open-cluster-management namespace"
        assert False, err_msg

    logger.info(f"Argocd route response : {argocd_route_response}")

    if argocd_route_response.status_code != 200:
        err_msg = "Argocd is not reachable. Please check the deployment"
        return False, err_msg
    else:
        return None


def validate_acm_self_registration_managed_clusters(openshift_dyn_client, kubefiles):
    err_msg = []
    for kubefile in kubefiles:
        kubefile_exp = os.path.expandvars(kubefile)
        with open(kubefile_exp) as stream:
            try:
                out = yaml.safe_load(stream)
                site_name = out["clusters"][0]["name"]
            except yaml.YAMLError:
                err_msg = "Failed to load kubeconfig file"
                assert False, err_msg

        logger.info(f"site_name: {site_name}")

        # Clusters provisioned with hcp will show name as "cluster" in kubeconfig
        # Check "server" value instead
        if site_name == "cluster":
            site_name = out["clusters"][0]["cluster"]["server"]
            logger.info(f"server: {site_name}")
            clusters = ManagedCluster.get(
                dyn_client=openshift_dyn_client, server=site_name
            )
        else:
            clusters = ManagedCluster.get(
                dyn_client=openshift_dyn_client, name=site_name
            )

        cluster = next(clusters)
        is_managed_cluster_joined, managed_cluster_status = cluster.self_registered

        logger.info(f"Cluster Managed : {is_managed_cluster_joined}")
        logger.info(f"Managed Cluster Status : {managed_cluster_status}")

        if not is_managed_cluster_joined:
            err_msg += f"{site_name} is not self registered"
        else:
            return None

    return err_msg


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
                    cmd = str(oc + cmdstring)
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
