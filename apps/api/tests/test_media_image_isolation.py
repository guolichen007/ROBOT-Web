import pytest

from scripts.check_test_image_isolation import check_isolation, mediamtx_image

PRODUCTION = "${FIREBOT_MEDIAMTX_IMAGE:-firebot-mediamtx:1.18.2}"


@pytest.mark.parametrize(
    "candidate",
    [
        "firebot-mediamtx:1.18.2",
        "${FIREBOT_TEST_MEDIAMTX_IMAGE:-firebot-mediamtx:1.18.2}",
        "firebot-mediamtx:ci",
        "${FIREBOT_MEDIAMTX_IMAGE:-firebot-test-mediamtx:1.18.2}",
    ],
)
def test_counterexample_production_tag_or_variable_reuse_fails(candidate):
    with pytest.raises(ValueError, match="production"):
        check_isolation(PRODUCTION, {"candidate": candidate})


def test_isolated_defaults_and_ci_tag_pass():
    defaults = check_isolation(
        PRODUCTION,
        {
            "test": "${FIREBOT_TEST_MEDIAMTX_IMAGE:-firebot-test-mediamtx:1.18.2}",
            "dev": "${FIREBOT_DEV_MEDIAMTX_IMAGE:-firebot-dev-mediamtx:1.18.2}",
            "ci": "firebot-test-mediamtx:ci",
        },
    )
    assert defaults["test"] != "firebot-mediamtx:1.18.2"


def test_service_image_is_not_confused_with_another_service():
    content = (
        "services:\n  redis:\n    image: redis:test\n"
        "  mediamtx:\n    image: isolated:test\n    build:\n      context: .\n"
        "  web:\n    image: web:test\n"
    )
    assert mediamtx_image(content) == "isolated:test"


def test_missing_image_cannot_borrow_the_next_service_image():
    with pytest.raises(ValueError, match="image missing"):
        mediamtx_image("services:\n  mediamtx:\n    build: .\n  web:\n    image: web:test\n")
