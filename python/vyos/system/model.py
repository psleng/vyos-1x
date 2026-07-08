# Copyright (C) 2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""igOS per-model definition resolver.

The single source of truth for a hardware model is one directory, shipped by
vyos-build and organised by processor family::

    <models_root>/<platform>/<name>/
        model.conf       key=value definition (identity + hardware); REQUIRED
        default-config   config.boot.default for this model;         optional
        cli-remove       newline list of CLI node paths to prune;    optional

``model.conf`` is a superset manifest.  Its keys::

    match     comma-separated list of EXACT ``<prod_id>-<model>`` ids this
              definition serves (matched verbatim; a profile may cover many
              order SKUs, e.g. IOLAN-2A00, IOLAN-2A01).           REQUIRED
    platform  processor family (am64x, j7200, ...).               REQUIRED
    dtb       device-tree blob this model boots (declared here so the whole
              model is defined in one place; the kernel builds it and U-Boot
              loads it — this file does not apply it).            optional
    serial    populated serial ports \u2014 consumed by vyos.hardware.board
    cell      none|socket|builtin                \u2014 consumed by vyos.hardware.board
    sim_mux   gpio|modem                          \u2014 consumed by vyos.hardware.board
    wifi      true|false                           \u2014 consumed by vyos.hardware.board
    ethernet, poe_pd, wifi_module                  \u2014 consumed by the build-time
              interfaces.conf / DTB generators (not by the runtime engine)
    fallback  ``true`` selects this definition for any unit whose id matches
              no definition (e.g. a board with no identity EEPROM, such as an
              EVM); config resolution only, at most one per platform. optional

Identity (``prod_id``, ``model``, and optional ``platform``) is resolved from
``/proc/cmdline`` first, then a ``product.env`` fallback \u2014 the same cascade the
init script already uses \u2014 so config selection and pin-map selection can never
disagree.  A model is selected when the running unit's ``<prod_id>-<model>`` id
appears verbatim in a definition's ``match`` list.  When nothing matches, a
platform-appropriate ``fallback = true`` definition may be used for config
resolution; failing that, ``None`` is returned so every caller safely keeps
its generic default.

This module is board-agnostic and cross-platform: it neither hard-codes model
names nor file-name conventions into its consumers.  ``vyos-router`` and
``vyos.hardware.board`` both *dereference* the resolved model directory instead
of constructing ``config-<id>`` / ``cli-remove-<id>`` paths themselves.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

# Where the build stages the per-model definition tree on the image.
DEFAULT_MODELS_ROOT = "/usr/share/igos/models"

# Identity token sources, in priority order.  cmdline wins (set by U-Boot from
# the board EEPROM); the product.env files are the fallback for units whose
# bootloader does not inject the tokens.
DEFAULT_CMDLINE = "/proc/cmdline"
DEFAULT_ENV_FILES = ("/mnt/efi/product.env", "/etc/product.env")

# Fixed file names inside a model directory (convention, not per-file config).
MODEL_CONF = "model.conf"
DEFAULT_CONFIG = "default-config"
CLI_REMOVE = "cli-remove"


def parse_conf(path: str) -> Dict[str, str]:
    """Parse a ``key = value`` file.  Missing/unreadable file -> ``{}``.

    Comments (``#``) and blank lines are ignored; surrounding whitespace is
    stripped.  Mirrors the format used by model.conf / product.env so there is
    one file grammar across the model tree.
    """
    result: Dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                result[key.strip()] = value.strip()
    except OSError:
        return {}
    return result


def _token_from_cmdline(cmdline: str, key: str) -> str:
    m = re.search(rf"(?:^|\s){re.escape(key)}=(\S+)", cmdline)
    return m.group(1) if m else ""


def resolve_identity(
    cmdline_path: str = DEFAULT_CMDLINE,
    env_files: tuple = DEFAULT_ENV_FILES,
) -> Dict[str, str]:
    """Resolve ``prod_id`` / ``model`` / optional ``platform`` for this unit.

    cmdline first (U-Boot injects ``prod_id=`` / ``model=`` \u2014 and optionally
    ``platform=`` \u2014 from the EEPROM), then each product.env fallback in turn.
    Returns a dict with keys ``prod_id``, ``model``, ``platform``, ``id``;
    ``id`` is ``"<prod_id>-<model>"`` when both are known, else ``""``.
    """
    prod_id = model = platform = ""
    try:
        with open(cmdline_path) as f:
            cmdline = f.read()
        prod_id = _token_from_cmdline(cmdline, "prod_id")
        model = _token_from_cmdline(cmdline, "model")
        platform = _token_from_cmdline(cmdline, "platform")
    except OSError:
        pass

    if not (prod_id and model):
        for env in env_files:
            env_conf = parse_conf(env)
            prod_id = prod_id or env_conf.get("prod_id", "")
            model = model or env_conf.get("model", "")
            platform = platform or env_conf.get("platform", "")
            if prod_id and model:
                break

    return {
        "prod_id": prod_id,
        "model": model,
        "platform": platform,
        "id": f"{prod_id}-{model}" if prod_id and model else "",
    }


@dataclass
class ModelDef:
    """A resolved model definition (one directory under the models root)."""

    name: str                    # directory name (friendly label only)
    platform: str                # processor family
    path: str                    # absolute path to the model directory
    conf: Dict[str, str]         # parsed model.conf (superset manifest)

    @property
    def model_conf(self) -> str:
        return os.path.join(self.path, MODEL_CONF)

    @property
    def default_config(self) -> Optional[str]:
        """config.boot.default for this model, or None if it ships none."""
        p = os.path.join(self.path, DEFAULT_CONFIG)
        return p if os.path.isfile(p) else None

    @property
    def cli_remove(self) -> Optional[str]:
        """CLI-node prune list for this model, or None if it ships none."""
        p = os.path.join(self.path, CLI_REMOVE)
        return p if os.path.isfile(p) else None

    @property
    def dtb(self) -> str:
        """Declared device-tree blob name (informational at runtime)."""
        return self.conf.get("dtb", "")

    def match_ids(self) -> List[str]:
        raw = self.conf.get("match", "")
        return [x.strip() for x in raw.split(",") if x.strip()]

    @property
    def is_fallback(self) -> bool:
        """True when this definition serves any unit that matched no id.

        Marked by ``fallback = true``.  Consulted only for config resolution
        (default-config / cli-remove); never for pin-map filtering, so an
        unidentified board keeps the full master hardware layer.
        """
        return self.conf.get("fallback", "").strip().lower() in (
            "true", "1", "yes")


def iter_models(models_root: str = DEFAULT_MODELS_ROOT) -> List[ModelDef]:
    """Load every ``<root>/<platform>/<name>/model.conf`` as a ModelDef."""
    models: List[ModelDef] = []
    pattern = os.path.join(models_root, "*", "*", MODEL_CONF)
    for conf_path in sorted(glob.glob(pattern)):
        model_dir = os.path.dirname(conf_path)
        conf = parse_conf(conf_path)
        models.append(
            ModelDef(
                name=os.path.basename(model_dir),
                platform=conf.get("platform", os.path.basename(
                    os.path.dirname(model_dir))),
                path=model_dir,
                conf=conf,
            )
        )
    return models


def _find_fallback(
    models: List[ModelDef], platform: str = "",
) -> Optional[ModelDef]:
    """Pick the ``fallback = true`` definition for a unit that matched no id.

    When ``platform`` is known it strictly scopes the search so an am64x image
    and a j7200 image never borrow each other's fallback.  A real image ships a
    single platform's models, so at most one fallback is present; more than one
    candidate after scoping raises, mirroring the exact-match duplicate check.
    """
    candidates = [m for m in models if m.is_fallback]
    if platform:
        candidates = [m for m in candidates if m.platform == platform]
    if not candidates:
        return None
    if len(candidates) > 1:
        paths = ", ".join(repr(m.path) for m in candidates)
        raise ValueError(
            f"multiple fallback model definitions ({paths}); at most one "
            "'fallback = true' definition may apply to a unit"
        )
    return candidates[0]


def find_model(
    models_root: str = DEFAULT_MODELS_ROOT,
    identity: Optional[Dict[str, str]] = None,
    allow_fallback: bool = False,
) -> Optional[ModelDef]:
    """Return the ModelDef whose ``match`` list contains this unit's id.

    When no id matches and ``allow_fallback`` is set, a ``fallback = true``
    definition is returned instead (config path only; ``False`` keeps pin-map
    resolution on the full master for unidentified boards).

    Exact, verbatim match on ``<prod_id>-<model>`` (no stripping, no globbing)
    so IRG-1000 and IRG-1A00 can never collide.  Returns None when the id is
    unknown or unmatched, so callers fall back to generic behaviour.  A
    duplicate id across two definitions raises \u2014 that is a build-data bug worth
    surfacing loudly rather than silently picking one.
    """
    if identity is None:
        identity = resolve_identity()
    wanted = identity.get("id", "")

    models = iter_models(models_root)
    hit: Optional[ModelDef] = None
    for m in models:
        if wanted and wanted in m.match_ids():
            if hit is not None:
                raise ValueError(
                    f"model id {wanted!r} matched by both {hit.path!r} and "
                    f"{m.path!r} \u2014 ambiguous model definitions"
                )
            hit = m
    if hit is not None:
        return hit

    if allow_fallback:
        return _find_fallback(models, identity.get("platform", ""))
    return None



def main() -> int:
    """CLI so shell consumers (e.g. the init script) can dereference a model
    without re-implementing identity resolution or path conventions."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Resolve this unit's igOS model definition."
    )
    parser.add_argument(
        "--models-root", default=DEFAULT_MODELS_ROOT,
        help=f"model definition tree (default: {DEFAULT_MODELS_ROOT})",
    )
    parser.add_argument(
        "--cmdline", default=DEFAULT_CMDLINE,
        help=f"kernel cmdline source (default: {DEFAULT_CMDLINE})",
    )
    parser.add_argument(
        "--env-file", action="append", dest="env_files", default=None,
        help="product.env fallback (repeatable; default: "
             f"{', '.join(DEFAULT_ENV_FILES)})",
    )
    parser.add_argument(
        "--field",
        choices=["dir", "default_config", "cli_remove", "dtb", "id",
                 "platform", "name"],
        help="print one resolved value (empty line if unavailable)",
    )
    parser.add_argument(
        "--sh", action="store_true",
        help="print shell-quoted MODEL_* assignments (one eval resolves all "
             "artifacts; empty value when unavailable)",
    )
    parser.add_argument(
        "--show", action="store_true",
        help="print the resolved identity and model directory",
    )
    args = parser.parse_args()

    env_files = tuple(args.env_files) if args.env_files else DEFAULT_ENV_FILES
    identity = resolve_identity(cmdline_path=args.cmdline, env_files=env_files)
    try:
        model = find_model(args.models_root, identity, allow_fallback=True)
    except ValueError as exc:
        print(f"E: {exc}", flush=True)
        return 2

    if args.sh:
        import shlex
        fields = {
            "MODEL_ID": identity["id"],
            "MODEL_PLATFORM": identity["platform"] or (
                model.platform if model else ""),
            "MODEL_NAME": model.name if model else "",
            "MODEL_DIR": model.path if model else "",
            "MODEL_DEFAULT_CONFIG": (model.default_config or "")
            if model else "",
            "MODEL_CLI_REMOVE": (model.cli_remove or "") if model else "",
            "MODEL_DTB": model.dtb if model else "",
        }
        for key, val in fields.items():
            print(f"{key}={shlex.quote(val)}")

    if args.show:
        print(f"id={identity['id']} platform={identity['platform']}")
        print(f"model_dir={model.path if model else ''}")

    if args.field:
        value = ""
        if args.field == "id":
            value = identity["id"]
        elif args.field == "platform":
            value = identity["platform"] or (model.platform if model else "")
        elif model is not None:
            value = {
                "dir": model.path,
                "default_config": model.default_config or "",
                "cli_remove": model.cli_remove or "",
                "dtb": model.dtb,
                "name": model.name,
            }[args.field]
        print(value)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
