"""The AI capability (ADR-0026): the prompt registry, and later the provider port and its execution.

It never holds a source fact, a product value, a final value, a provider SDK or a secret, and
COLLECT never imports it (ADR-0026 §2, AIF-02).
"""
