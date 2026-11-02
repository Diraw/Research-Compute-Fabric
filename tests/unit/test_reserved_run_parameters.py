"""Caller parameters cannot replace executor-owned template values."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from rcf.core.models import RunRequest


@pytest.mark.parametrize("name", ["run_id", "project_id", "node_id", "python", "output_dir", "params_file", "script", "launcher"])
def test_reserved_template_parameters_are_rejected(name):
    with pytest.raises(ValidationError, match="reserved"):
        RunRequest(project_id="demo", params={name: "override"})


def test_regular_scalar_parameters_remain_supported():
    request = RunRequest(project_id="demo", params={"seed": 1, "steps": 100, "label": "sample", "enabled": True})
    assert request.params["seed"] == 1


def test_planner_rejects_reserved_params_even_after_model_copy(tmp_path):
    from conftest import make_home

    from rcf.core.config import Config
    from rcf.core.errors import SpecError
    from rcf.core.runner import ScriptedRunner
    from rcf.service import RcfService

    home = make_home(tmp_path, nodes='[nodes.local]\nkind="local"\n',
                     projects='[projects.demo]\npaths={local="/checkout"}\n')
    request = RunRequest(project_id="demo").model_copy(update={"params": {"python": "other-runtime"}})
    with RcfService(Config.load(home), runner=ScriptedRunner()) as svc:
        with pytest.raises(SpecError, match="reserved"):
            svc.planner._assemble(request=request, project=svc.config.project("demo"),
                                  node=svc.config.node("local"), snapshot=None,
                                  checks=[], env_view=None, env_id=None)
