"""Google Drive integration package scaffold.

Future implementation will handle OAuth 2.0 token management, folder sync,
and worksheet document storage.
"""

from packages.drive.client import GoogleDriveClient

__all__ = ["GoogleDriveClient"]
