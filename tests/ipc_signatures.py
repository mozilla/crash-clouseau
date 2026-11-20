# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Two IPC messages that share a transport frame in the test signatures."""

IPC_SEND = "mozilla::ipc::PortLink::SendMessage"
IDB_MESSAGE = "PBackgroundIDBRequest::Msg___delete__"
LS_MESSAGE = "PBackgroundLSSnapshot::Reply_LoadKeys"
IDB_ANNOTATION = f"IPC_Message_Name={IDB_MESSAGE}"
LS_ANNOTATION = f"IPC_Message_Name={LS_MESSAGE}"
IDB_SIGNATURE = f"{IPC_SEND} | {IDB_ANNOTATION}"
LS_SIGNATURE = f"{IPC_SEND} | {LS_ANNOTATION}"
