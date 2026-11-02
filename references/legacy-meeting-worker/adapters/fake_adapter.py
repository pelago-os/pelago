# Historical reference only. Not integrated or supported for deployment. See README.md.
def run_fake_adapter(prompt: str, timeout: int) -> str:
    """
    Fake adapter for X1 tests.
    It echoes the prompt or a predefined string.
    """
    if "X1_OK" in prompt:
        return "X1_OK"
    return f"ECHO: {prompt}"
