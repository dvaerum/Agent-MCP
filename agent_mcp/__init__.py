"""
Agent-MCP: Multi-Agent Collaboration Protocol for AI software development.

Copyright (C) 2025 Luis Alejandro Rincon (rinadelph)

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from importlib.metadata import PackageNotFoundError, version as _version

# pyproject.toml is the single source of truth for the version; read it back
# from the installed package metadata rather than duplicating a literal here
# (the old hand-maintained "2.2.0" silently drifted years behind pyproject).
try:
    __version__ = _version("agent-mcp")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+source"
