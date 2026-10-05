# SPDX-License-Identifier: AGPL-3.0-only
"""Production LlmClient adapter implementations (PR 10).

Provider-backed LlmClient implementations live only here, behind the
existing :class:`~darkula.app.llm.LlmClient` boundary; application/domain
code never imports this package and never sees provider types.
"""

from darkula.infrastructure.llm.openai import OpenAiLlmClient

__all__ = ["OpenAiLlmClient"]
