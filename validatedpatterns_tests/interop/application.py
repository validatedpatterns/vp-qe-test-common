import logging

from ocp_resources.route import Route
from openshift.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import NotFoundError


from . import __loggername__
from .crd import ArgoCD


logger = logging.getLogger(__loggername__)


def get_site_api_url(openshift_dyn_client: DynamicClient) -> str:
    """
    Return the host url from dynamic client configuration

    :param openshift_dyn_client: The openshift DynamicClient to connect to the cluster
    """
    if not openshift_dyn_client.configuration.host:
        raise RuntimeError("Hub site url is missing in kubeconfig file")
    return openshift_dyn_client.configuration.host


def get_route_url(
    openshift_dyn_client: DynamicClient, namespace: str, name: str
) -> str:
    """
    Return the url of a route

    :param openshift_dyn_client: The openshift DynamicClient to connect to the cluster
    :param namespace: The namespace to find the route
    :param name: The name of the route
    :return url: The url of the route, prefixed with http/https
    """
    try:
        route = next(
            Route.get(
                dyn_client=openshift_dyn_client,
                namespace=namespace,
                name=name,
            )
        )
    except NotFoundError:
        raise RuntimeError(f"Route '{name}' was not found in namespace '{namespace}'.")
    
    scheme = "https" if getattr(route.instance.spec, "tls", None) else "http"
    return f"{scheme}://{route.instance.spec.host}"



def assert_argocd_applications(openshift_dyn_client, projects):
    unhealthy_apps = []

    for project in projects:
        for app in ArgoCD.get(
            dyn_client=openshift_dyn_client,
            namespace=project,
        ):
            app_name = app.instance.metadata.name
            app_health = app.instance.status.health.status
            app_sync = app.instance.status.sync.status

            if app_health != "Healthy" or app_sync != "Synced":
                details = [f"{app_name}: health={app_health}, sync={app_sync}"]

                resources = getattr(app.instance.status, "resources", None)
                if resources:
                    for res in resources:
                        health = getattr(getattr(res, "health", None), "status", "N/A")
                        sync = getattr(res, "status", "Unknown")

                        if health != "Healthy" or sync != "Synced":
                            details.append(
                                f"  - {res.kind}/{res.name}: health={health}, sync={sync}"
                            )
                else:
                    details.append("  - No resources found for app")
                unhealthy_apps.append("\n".join(details))

    assert not unhealthy_apps, (
        "The following Argo CD applications are unhealthy:\n\n"
        + "\n\n".join(unhealthy_apps)
    )
