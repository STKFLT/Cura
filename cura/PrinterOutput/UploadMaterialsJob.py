# Copyright (c) 2021 Ultimaker B.V.
# Cura is released under the terms of the LGPLv3 or higher.

import enum
import functools
import json  # To serialise metadata for API calls.
import os  # To delete the archive when we're done.
from PyQt5.QtCore import QUrl
import tempfile  # To create an archive before we upload it.

import cura.CuraApplication  # Imported like this to prevent circular imports.
from cura.Settings.CuraContainerRegistry import CuraContainerRegistry  # To find all printers to upload to.
from cura.UltimakerCloud import UltimakerCloudConstants  # To know where the API is.
from cura.UltimakerCloud.UltimakerCloudScope import UltimakerCloudScope  # To know how to communicate with this server.
from UM.i18n import i18nCatalog
from UM.Job import Job
from UM.Logger import Logger
from UM.Signal import Signal
from UM.TaskManagement.HttpRequestManager import HttpRequestManager  # To call the API.
from UM.TaskManagement.HttpRequestScope import JsonDecoratorScope

from typing import Dict, Optional, TYPE_CHECKING
if TYPE_CHECKING:
    from PyQt5.QtNetwork import QNetworkReply
    from cura.UltimakerCloud.CloudMaterialSync import CloudMaterialSync

catalog = i18nCatalog("cura")


class UploadMaterialsError(Exception):
    """
    Class to indicate something went wrong while uploading.
    """
    pass


class UploadMaterialsJob(Job):
    """
    Job that uploads a set of materials to the Digital Factory.
    """

    UPLOAD_REQUEST_URL = f"{UltimakerCloudConstants.CuraCloudAPIRoot}/connect/v1/materials/upload"
    UPLOAD_CONFIRM_URL = UltimakerCloudConstants.CuraCloudAPIRoot + "/connect/v1/clusters/{cluster_id}/printers/{cluster_printer_id}/action/confirm_material_upload"

    class Result(enum.IntEnum):
        SUCCCESS = 0
        FAILED = 1

    def __init__(self, material_sync: "CloudMaterialSync"):
        super().__init__()
        self._material_sync = material_sync
        self._scope = JsonDecoratorScope(UltimakerCloudScope(cura.CuraApplication.CuraApplication.getInstance()))  # type: JsonDecoratorScope
        self._archive_filename = None  # type: Optional[str]
        self._archive_remote_id = None  # type: Optional[str]  # ID that the server gives to this archive. Used to communicate about the archive to the server.
        self._printer_sync_status = {}
        self._printer_metadata = {}
        self.processProgressChanged.connect(self._onProcessProgressChanged)

    uploadCompleted = Signal()
    processProgressChanged = Signal()
    uploadProgressChanged = Signal()

    def run(self):
        self._printer_metadata = CuraContainerRegistry.getInstance().findContainerStacksMetadata(
            type = "machine",
            connection_type = "3",  # Only cloud printers.
            is_online = "True",  # Only online printers. Otherwise the server gives an error.
            host_guid = "*",  # Required metadata field. Otherwise we get a KeyError.
            um_cloud_cluster_id = "*"  # Required metadata field. Otherwise we get a KeyError.
        )
        for printer in self._printer_metadata:
            self._printer_sync_status[printer["host_guid"]] = "uploading"

        archive_file = tempfile.NamedTemporaryFile("wb", delete = False)
        archive_file.close()
        self._archive_filename = archive_file.name

        self._material_sync.exportAll(QUrl.fromLocalFile(self._archive_filename), notify_progress = self.processProgressChanged)
        file_size = os.path.getsize(self._archive_filename)

        request_metadata = {
            "data": {
                "file_size": file_size,
                "file_name": "cura.umm",  # File name can be anything as long as it's .umm. It's not used by anyone.
                "content_type": "application/zip",  # This endpoint won't receive files of different MIME types.
                "origin": "cura"  # Some identifier against hackers intercepting this upload request, apparently.
            }
        }
        request_payload = json.dumps(request_metadata).encode("UTF-8")

        http = HttpRequestManager.getInstance()
        http.put(
            url = self.UPLOAD_REQUEST_URL,
            data = request_payload,
            callback = self.onUploadRequestCompleted,
            error_callback = self.onError,
            scope = self._scope
        )

    def onUploadRequestCompleted(self, reply: "QNetworkReply", error: Optional["QNetworkReply.NetworkError"]):
        response_data = HttpRequestManager.readJSON(reply)
        if response_data is None:
            Logger.error(f"Invalid response to material upload request. Could not parse JSON data.")
            self.failed(UploadMaterialsError(catalog.i18nc("@text:error", "The response from Digital Factory appears to be corrupted.")))
            return
        if "upload_url" not in response_data:
            Logger.error(f"Invalid response to material upload request: Missing 'upload_url' field to upload archive to.")
            self.failed(UploadMaterialsError(catalog.i18nc("@text:error", "The response from Digital Factory is missing important information.")))
            return
        if "material_profile_id" not in response_data:
            Logger.error(f"Invalid response to material upload request: Missing 'material_profile_id' to communicate about the materials with the server.")
            self.failed(UploadMaterialsError(catalog.i18nc("@text:error", "The response from Digital Factory is missing important information.")))
            return

        upload_url = response_data["upload_url"]
        self._archive_remote_id = response_data["material_profile_id"]
        file_data = open(self._archive_filename, "rb").read()
        http = HttpRequestManager.getInstance()
        http.put(
            url = upload_url,
            data = file_data,
            callback = self.onUploadCompleted,
            error_callback = self.onError,
            scope = self._scope
        )

    def onUploadCompleted(self, reply: "QNetworkReply", error: Optional["QNetworkReply.NetworkError"]):
        for container_stack in self._printer_metadata:
            cluster_id = container_stack["um_cloud_cluster_id"]
            printer_id = container_stack["host_guid"]

            http = HttpRequestManager.getInstance()
            http.get(
                url = self.UPLOAD_CONFIRM_URL.format(cluster_id = cluster_id, cluster_printer_id = printer_id),
                callback = functools.partialmethod(self.onUploadConfirmed, printer_id),
                error_callback = functools.partialmethod(self.onUploadConfirmed, printer_id),  # Let this same function handle the error too.
                scope = self._scope
            )

    def onUploadConfirmed(self, printer_id: str, reply: "QNetworkReply", error: Optional["QNetworkReply.NetworkError"]) -> None:
        if error is not None:
            Logger.error(f"Failed to confirm uploading material archive to printer {printer_id}: {error}")
            self._printer_sync_status[printer_id] = "failed"
        else:
            self._printer_sync_status[printer_id] = "success"

        still_uploading = len([val for val in self._printer_sync_status.values() if val == "uploading"])
        self.uploadProgressChanged.emit(0.8 + (len(self._printer_sync_status) - still_uploading) / len(self._printer_sync_status), self.getPrinterSyncStatus())

        if still_uploading == 0:  # This is the last response to be processed.
            if "failed" in self._printer_sync_status.values():
                self.setResult(self.Result.FAILED)
                self.setError(UploadMaterialsError(catalog.i18nc("@text:error", "Failed to connect to Digital Factory to sync materials with some of the printers.")))
            else:
                self.setResult(self.Result.SUCCESS)
            self.uploadCompleted.emit(self.getResult(), self.getError())

    def onError(self, reply: "QNetworkReply", error: Optional["QNetworkReply.NetworkError"]):
        Logger.error(f"Failed to upload material archive: {error}")
        self.failed(UploadMaterialsError(catalog.i18nc("@text:error", "Failed to connect to Digital Factory.")))

    def getPrinterSyncStatus(self) -> Dict[str, str]:
        return self._printer_sync_status

    def failed(self, error: UploadMaterialsError) -> None:
        """
        Helper function for when we have a general failure.

        This sets the sync status for all printers to failed, sets the error on
        the job and the result of the job to FAILED.
        :param error: An error to show to the user.
        """
        self.setResult(self.Result.FAILED)
        self.setError(error)
        for printer_id in self._printer_sync_status:
            self._printer_sync_status[printer_id] = "failed"
        self.uploadProgressChanged.emit(1.0, self.getPrinterSyncStatus())
        self.uploadCompleted.emit(self.getResult(), self.getError())

    def _onProcessProgressChanged(self, progress: float) -> None:
        self.uploadProgressChanged.emit(progress * 0.8, self.getPrinterSyncStatus())  # The processing is 80% of the progress bar.
