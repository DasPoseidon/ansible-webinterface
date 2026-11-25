"""Dateiverwaltung, Editor und Git-Historie."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form, HTTPException,
                     Query, Request, UploadFile, status)
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .auth import Principal
from .deps import client_ip, get_services, require
from .files import AccessDenied, Conflict, FileError
from .gitrepo import GitError

router = APIRouter(prefix="/api", tags=["Dateien"])


class SaveBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=20_000_000)
    sha256: Optional[str] = None
    message: str = Field(default="", max_length=500)
    skip_validation: bool = False


class ValidateBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=20_000_000)


class NameBody(BaseModel):
    path: str = Field(min_length=0, max_length=4096)
    name: str = Field(min_length=1, max_length=255)


class CommitBody(BaseModel):
    message: str = Field(default="", max_length=500)


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, Conflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, AccessDenied):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, FileError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, GitError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                             detail="Git: %s" % exc)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


# -- Durchsehen und lesen --------------------------------------------------


@router.get("/files")
async def list_files(path: str = "", found: Principal = Depends(require("files.read"))) -> Dict[str, Any]:
    services = get_services()
    try:
        return services.files.list_dir(path)
    except (FileError, AccessDenied) as exc:
        raise _handle(exc)


@router.get("/files/content")
async def read_file(path: str, found: Principal = Depends(require("files.read"))) -> Dict[str, Any]:
    services = get_services()
    try:
        return services.files.read_file(path)
    except (FileError, AccessDenied) as exc:
        raise _handle(exc)


@router.get("/files/backup")
async def read_backup(path: str, stamp: str,
                      found: Principal = Depends(require("files.read"))) -> Dict[str, Any]:
    services = get_services()
    try:
        return services.files.read_backup(path, stamp)
    except (FileError, AccessDenied) as exc:
        raise _handle(exc)


@router.get("/files/download")
async def download_file(path: str, found: Principal = Depends(require("files.read"))):
    services = get_services()
    try:
        target = services.files.download_file(path)
    except (FileError, AccessDenied) as exc:
        raise _handle(exc)
    return FileResponse(str(target), filename=target.name,
                        media_type="application/octet-stream")


@router.get("/files/zip")
async def download_zip(path: str, background: BackgroundTasks,
                       found: Principal = Depends(require("files.read"))):
    services = get_services()
    try:
        archive, name = await asyncio.to_thread(services.files.zip_directory, path)
    except (FileError, AccessDenied) as exc:
        raise _handle(exc)
    background.add_task(lambda: archive.unlink(missing_ok=True))
    return FileResponse(str(archive), filename=name, media_type="application/zip",
                        background=background)


# -- Schreiben -------------------------------------------------------------


@router.post("/files/validate")
async def validate_content(body: ValidateBody,
                           found: Principal = Depends(require("files.read"))) -> Dict[str, Any]:
    """Prueft Inhalte, ohne etwas zu speichern."""
    services = get_services()
    try:
        target = services.files.resolve(body.path, must_exist=False)
    except (FileError, AccessDenied) as exc:
        raise _handle(exc)
    result = await services.validator.check_text(target, body.content)
    return result.to_dict()


@router.put("/files/content")
async def save_file(body: SaveBody, request: Request,
                    found: Principal = Depends(require("files.write"))) -> Dict[str, Any]:
    services = get_services()
    if body.skip_validation and not found.can("users.manage"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Die Pruefung darf nur ein Administrator ueberspringen")
    try:
        result = await services.files.save_file(
            body.path, body.content, user=found.name, expected_sha=body.sha256,
            message=body.message, skip_validation=body.skip_validation)
    except (FileError, AccessDenied, Conflict) as exc:
        raise _handle(exc)
    if result.get("saved"):
        services.inventory.invalidate()
        services.audit.write(found.name, "datei.gespeichert", body.path,
                             detail={"commit": (result.get("git") or {}).get("commit"),
                                     "neu": result.get("created")},
                             ip=client_ip(request))
    else:
        services.audit.write(found.name, "datei.abgelehnt", body.path, ok=False,
                             detail={"grund": result.get("validation", {}).get("messages")},
                             ip=client_ip(request))
    return result


@router.post("/files/dir", status_code=status.HTTP_201_CREATED)
async def create_dir(body: NameBody, request: Request,
                     found: Principal = Depends(require("files.write"))) -> Dict[str, Any]:
    services = get_services()
    try:
        result = await services.files.create_dir(body.path, body.name, user=found.name)
    except (FileError, AccessDenied) as exc:
        raise _handle(exc)
    services.audit.write(found.name, "ordner.angelegt", result["path"], ip=client_ip(request))
    return result


@router.post("/files/rename")
async def rename_entry(body: NameBody, request: Request,
                       found: Principal = Depends(require("files.write"))) -> Dict[str, Any]:
    services = get_services()
    try:
        result = await services.files.rename(body.path, body.name, user=found.name)
    except (FileError, AccessDenied) as exc:
        raise _handle(exc)
    services.inventory.invalidate()
    services.audit.write(found.name, "datei.umbenannt", body.path,
                         detail={"neu": result["path"]}, ip=client_ip(request))
    return result


@router.delete("/files")
async def delete_entry(path: str, request: Request, recursive: bool = False,
                       found: Principal = Depends(require("files.delete"))) -> Dict[str, Any]:
    services = get_services()
    try:
        result = await services.files.delete(path, user=found.name, recursive=recursive)
    except (FileError, AccessDenied) as exc:
        raise _handle(exc)
    services.inventory.invalidate()
    services.audit.write(found.name, "datei.geloescht", path,
                         detail={"rekursiv": recursive}, ip=client_ip(request))
    return result


@router.post("/files/upload")
async def upload_files(request: Request, path: str = Form(default=""),
                       overwrite: bool = Form(default=False),
                       files: List[UploadFile] = File(...),
                       found: Principal = Depends(require("files.upload"))) -> Dict[str, Any]:
    services = get_services()
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for upload in files:
        try:
            result = await services.files.upload(path, upload.filename or "", upload.file,
                                                 user=found.name, overwrite=overwrite)
            results.append(result)
            services.audit.write(found.name, "datei.hochgeladen", result["path"],
                                 detail={"groesse": result["size"],
                                         "ersetzt": result["replaced"]},
                                 ip=client_ip(request))
        except (FileError, AccessDenied) as exc:
            errors.append({"name": upload.filename or "?", "error": str(exc)})
        finally:
            await upload.close()
    services.inventory.invalidate()
    return {"uploaded": results, "errors": errors}


# -- Git -------------------------------------------------------------------


@router.get("/git")
async def git_info(found: Principal = Depends(require("git.read"))) -> Dict[str, Any]:
    services = get_services()
    try:
        return await services.git.info()
    except GitError as exc:
        raise _handle(exc)


@router.get("/git/log")
async def git_log(path: Optional[str] = None, limit: int = Query(default=50, ge=1, le=500),
                  found: Principal = Depends(require("git.read"))) -> Dict[str, Any]:
    services = get_services()
    target: Optional[Path] = None
    if path:
        try:
            target = services.files.resolve(path, must_exist=False)
        except (FileError, AccessDenied) as exc:
            raise _handle(exc)
    return {"commits": await services.git.log(target, limit)}


@router.get("/git/diff", response_class=PlainTextResponse)
async def git_diff(path: Optional[str] = None, revision: Optional[str] = None,
                   found: Principal = Depends(require("git.read"))) -> str:
    services = get_services()
    target: Optional[Path] = None
    if path:
        try:
            target = services.files.resolve(path, must_exist=False)
        except (FileError, AccessDenied) as exc:
            raise _handle(exc)
    if revision:
        if not revision.replace("^", "").replace("~", "").isalnum():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Ungueltige Commit-Angabe")
        return await services.git.diff_commit(revision, target)
    return await services.git.diff_working(target)


@router.get("/git/file")
async def git_file(path: str, revision: str,
                   found: Principal = Depends(require("git.read"))) -> Dict[str, Any]:
    services = get_services()
    if not revision.replace("^", "").replace("~", "").isalnum():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Ungueltige Commit-Angabe")
    try:
        target = services.files.resolve(path, must_exist=False)
        content = await services.git.file_at(target, revision)
    except (FileError, AccessDenied, GitError) as exc:
        raise _handle(exc)
    return {"path": services.files.rel(target), "revision": revision, "content": content}


@router.post("/git/commit")
async def git_commit_all(body: CommitBody, request: Request,
                         found: Principal = Depends(require("git.write"))) -> Dict[str, Any]:
    """Committet alle offenen Aenderungen im Ansible-Verzeichnis."""
    services = get_services()
    if not services.git.enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Git ist nicht aktiv")
    message = body.message.strip() or "Sammelcommit aus dem Webinterface"
    result = await services.git.commit_paths(
        [services.cfg.ansible_dir], "%s\n\nVon %s ueber das Ansible-Webinterface."
        % (message, found.name), found.name)
    services.audit.write(found.name, "git.commit", "(alle Aenderungen)",
                         detail=result, ip=client_ip(request))
    return result


@router.post("/git/push")
async def git_push(request: Request,
                   found: Principal = Depends(require("git.write"))) -> Dict[str, Any]:
    services = get_services()
    if not services.git.enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Git ist nicht aktiv")
    result = await services.git.push(found.name)
    services.audit.write(found.name, "git.push", services.cfg.git_cfg.get("remote", "origin"),
                         ok=result.get("ok", False), detail=result, ip=client_ip(request))
    return result
