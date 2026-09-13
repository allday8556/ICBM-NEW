from typing import Annotated, cast

from fastapi import Depends, Request

from app.container import Container


def get_container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


ContainerDep = Annotated[Container, Depends(get_container)]
