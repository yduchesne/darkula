# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula deterministic testing doubles.

Reusable, offline fakes that implement the real Darkula interfaces (never a
parallel fake architecture): :class:`FakeLlmClient`,
:class:`FakeDataStream`, and (through
:mod:`darkula.infrastructure.object_store`) InMemoryObjectStore.
"""
