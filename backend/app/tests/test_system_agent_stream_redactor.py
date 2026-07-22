from app.services.system_agent.redactor import StreamingMessageRedactor


def test_streaming_redactor_hides_known_secret_split_across_deltas() -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    redactor = StreamingMessageRedactor(secrets=[secret])

    output = "".join(
        [
            redactor.push("结果 key="),
            redactor.push(secret[:12]),
            redactor.push(secret[12:] + " 完成"),
            redactor.finish(),
        ]
    )

    assert secret not in output
    assert output == "结果 key=[REDACTED] 完成"


def test_streaming_redactor_hides_unknown_bearer_split_across_deltas() -> None:
    token = "abcdefghijklmnopqrstuvwxyz.1234567890.signature"
    redactor = StreamingMessageRedactor()

    output = "".join(
        [
            redactor.push("Authorization: Bear"),
            redactor.push("er " + token[:15]),
            redactor.push(token[15:] + "\n完成"),
            redactor.finish(),
        ]
    )

    assert token not in output
    assert "Bearer [REDACTED]" in output


def test_streaming_redactor_hides_unknown_provider_tokens_at_finish() -> None:
    tokens = (
        "xai-abcdefghijklmnopqrstuvwxyz123456",
        "gsk_abcdefghijklmnopqrstuvwxyz123456",
        "AIzaabcdefghijklmnopqrstuvwxyz123456",
    )

    for token in tokens:
        redactor = StreamingMessageRedactor()
        output = redactor.push(f"模型输出 {token}") + redactor.finish()

        assert token not in output
        assert output == "模型输出 [REDACTED]"


def test_streaming_redactor_hides_unknown_provider_token_split_across_deltas() -> None:
    token = "xai-abcdefghijklmnopqrstuvwxyz123456"
    redactor = StreamingMessageRedactor()

    output = "".join(
        (
            redactor.push("模型输出 xai-abcdefgh"),
            redactor.push("ijklmnopqrstuv"),
            redactor.push("wxyz123456 完成"),
            redactor.finish(),
        )
    )

    assert token not in output
    assert output == "模型输出 [REDACTED] 完成"
