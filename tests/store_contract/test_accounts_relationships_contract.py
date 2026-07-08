from __future__ import annotations


def test_account_bootstrap_dev_token_and_login_contract(store):
    default = store.get_account(account_id="user_default")
    assert default is not None
    assert default.status == "active"
    assert default.roles == ["user"]

    account = store.upsert_account(
        account_id="user_1",
        username="user_1",
        display_name="User One",
        status="active",
        roles=["user", "tester"],
        dev_token_hash="hash_1",
    )
    assert account.roles == ["user", "tester"]
    assert store.get_account_by_dev_token_hash(dev_token_hash="hash_1").id == "user_1"

    updated = store.set_account_dev_token_hash(
        account_id="user_1",
        dev_token_hash="hash_2",
    )
    assert updated.dev_token_hash == "hash_2"
    assert store.get_account_by_dev_token_hash(dev_token_hash="hash_1") is None
    assert store.get_account_by_dev_token_hash(dev_token_hash="hash_2").id == "user_1"

    disabled = store.set_account_status(account_id="user_1", status="disabled")
    assert disabled.status == "disabled"
    logged_in = store.touch_account_login(account_id="user_1")
    assert logged_in.last_login_at is not None


def test_relationship_state_create_update_and_clamp_contract(store):
    initial = store.get_or_create_relationship_state(
        user_id="user_1",
        character_id="aemeath",
    )
    assert initial.trust == 50
    assert initial.affection == 50
    assert initial.familiarity == 50

    updated = store.update_relationship_state(
        user_id="user_1",
        character_id="aemeath",
        mood="warm",
        user_emotion="happy",
        trust_delta=80,
        affection_delta=-80,
        familiarity_delta=5,
        last_topic="contract tests",
    )
    assert updated.mood == "warm"
    assert updated.user_emotion == "happy"
    assert updated.trust == 100
    assert updated.affection == 0
    assert updated.familiarity == 55
    assert updated.last_topic == "contract tests"
