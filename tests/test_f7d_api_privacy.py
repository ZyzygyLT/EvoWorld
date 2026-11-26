from __future__ import annotations

import json

import pytest

from worldsim.server.db import ChallengeRow, ChallengeParticipantRow, UserRow


def _register(client, username):
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "test-password"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    return {
        "Authorization": f"Bearer {data['token']}"
    }, data["user_id"]


def _challenge(env, organizer_id, state="published"):
    with env.Session() as session:
        row = ChallengeRow(
            organizer_user_id=organizer_id,
            name="Privacy test",
            config_json=json.dumps({"primary_metric": "total_population"}),
            rules_version="1.0",
            duration_ticks=50,
            state=state,
            created_at="2026-11-23 00:00:00",
        )
        session.add(row)
        session.commit()
        return row.id


def _participant(env, challenge_id, user_id, world_id, *,
                 status="confirmed", alias="Alias", population=100,
                 result_verified=None):
    with env.Session() as session:
        metrics = {} if population is None else {
            "total_population": population,
            "tech_level": 2,
            "wars_won": 1,
        }
        row = ChallengeParticipantRow(
            challenge_id=challenge_id,
            user_id=user_id,
            world_id=world_id,
            public_alias=alias,
            result_json=json.dumps(metrics),
            result_status=status,
            result_verified=(
                int(status in ("verified", "confirmed"))
                if result_verified is None else result_verified
            ),
            state="activo",
        )
        session.add(row)
        session.commit()
        return row.id


def test_public_challenge_list_hides_organizer_and_unpublished(f7c_api):
    client, env = f7c_api
    _, organizer_id = _register(client, "listowner")
    published_id = _challenge(env, organizer_id, "published")
    _challenge(env, organizer_id, "draft")

    response = client.get("/api/challenges")

    assert response.status_code == 200, response.text
    rows = response.json()["challenges"]
    ids = {row["id"] for row in rows}
    assert published_id in ids
    assert all(row["state"] == "published" for row in rows)
    assert all("organizer_user_id" not in row for row in rows)


def test_challenge_detail_visibility_and_organizer_privacy(f7c_api):
    client, env = f7c_api
    owner_headers, owner_id = _register(client, "detailowner")
    outsider_headers, _ = _register(client, "detailoutsider")

    published_id = _challenge(env, owner_id, "published")
    draft_id = _challenge(env, owner_id, "draft")

    public_response = client.get(f"/api/challenges/{published_id}")
    assert public_response.status_code == 200
    assert "organizer_user_id" not in public_response.json()

    owner_response = client.get(
        f"/api/challenges/{draft_id}", headers=owner_headers
    )
    assert owner_response.status_code == 200
    assert "organizer_user_id" not in owner_response.json()

    assert client.get(f"/api/challenges/{draft_id}").status_code == 404
    assert client.get(
        f"/api/challenges/{draft_id}", headers=outsider_headers
    ).status_code == 404


def test_participants_require_authentication_and_limit_rows(f7c_api):
    client, env = f7c_api
    owner_headers, owner_id = _register(client, "partowner")
    participant_headers, participant_id = _register(client, "partuser")
    outsider_headers, _ = _register(client, "partoutsider")

    challenge_id = _challenge(env, owner_id, "running")
    _participant(env, challenge_id, participant_id, 81001)
    _participant(env, challenge_id, owner_id, 81002, alias="Owner")
    _participant(env, challenge_id, 999999, 81003, alias="Other")

    url = f"/api/challenges/{challenge_id}/participants"

    assert client.get(url).status_code == 401
    assert client.get(
        "/api/challenges/999999/participants",
        headers=participant_headers,
    ).status_code == 404

    owner_response = client.get(url, headers=owner_headers)
    assert owner_response.status_code == 200
    assert len(owner_response.json()["participants"]) == 3

    participant_response = client.get(url, headers=participant_headers)
    assert participant_response.status_code == 200
    rows = participant_response.json()["participants"]
    assert len(rows) == 1
    assert rows[0]["user_id"] == participant_id
    assert all("state_json" not in row for row in rows)
    assert all("joined_at" not in row for row in rows)

    assert client.get(url, headers=outsider_headers).status_code == 404


def test_admin_can_view_all_participants_based_on_database_role(f7c_api):
    client, env = f7c_api
    owner_headers, owner_id = _register(client, "adminpartowner")
    admin_headers, admin_id = _register(client, "adminpartuser")
    _, other_id = _register(client, "adminpartother")

    with env.Session() as session:
        admin = session.get(UserRow, admin_id)
        admin.is_admin = 1
        session.commit()

    challenge_id = _challenge(env, owner_id, "running")
    _participant(env, challenge_id, admin_id, 82001)
    _participant(env, challenge_id, other_id, 82002)

    response = client.get(
        f"/api/challenges/{challenge_id}/participants",
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert len(response.json()["participants"]) == 2


def test_public_results_are_anonymous_and_confirmed_only(f7c_api):
    client, env = f7c_api
    _, owner_id = _register(client, "resultowner")
    challenge_id = _challenge(env, owner_id, "completed")

    _participant(
        env, challenge_id, 101, 83001,
        status="confirmed", alias="Public-A", population=300,
    )
    _participant(
        env, challenge_id, 102, 83002,
        status="persisted", alias="Private-B", population=500,
    )
    _participant(
        env, challenge_id, 103, 83003,
        status="confirmed", alias="Public-C", population=200,
    )
    _participant(
        env, challenge_id, 104, 83004,
        status="verified", alias="Verified-D", population=600,
    )

    response = client.get(f"/api/challenges/{challenge_id}/results")
    assert response.status_code == 200, response.text

    rows = response.json()["participants"]
    assert [row["alias"] for row in rows] == ["Public-A", "Public-C"]
    assert [row["position"] for row in rows] == [1, 2]

    forbidden = {"user_id", "world_id", "result_json", "state_json"}
    for row in rows:
        assert forbidden.isdisjoint(row)
        assert set(row["metrics"]) <= {
            "total_population", "wars_won", "tech_level", "influence_max"
        }

    # El JSON persistido puede contener campos internos o añadidos en el futuro;
    # la respuesta pública debe mantener una allowlist estricta.
    with env.Session() as session:
        participant = session.query(ChallengeParticipantRow).filter_by(
            challenge_id=challenge_id, public_alias="Public-A"
        ).one()
        persisted_metrics = json.loads(participant.result_json)
        persisted_metrics["internal_score"] = 999
        persisted_metrics["private_note"] = "must-not-leak"
        participant.result_json = json.dumps(persisted_metrics)
        session.commit()

    filtered_response = client.get(f"/api/challenges/{challenge_id}/results")
    assert filtered_response.status_code == 200, filtered_response.text
    public_a = next(
        row for row in filtered_response.json()["participants"]
        if row["alias"] == "Public-A"
    )
    assert public_a["metrics"] == {
        "total_population": 300, "tech_level": 2, "wars_won": 1
    }

    outsider_headers, _ = _register(client, "resultoutsider")
    outsider_response = client.get(
        f"/api/challenges/{challenge_id}/results",
        headers=outsider_headers,
    )
    assert outsider_response.status_code == 200, outsider_response.text
    outsider_rows = outsider_response.json()["participants"]
    assert outsider_rows == rows
    for row in outsider_rows:
        assert forbidden.isdisjoint(row)


def test_participant_can_view_own_pending_row_before_completion(f7c_api):
    client, env = f7c_api
    owner_headers, owner_id = _register(client, "ownresultowner")
    participant_headers, participant_id = _register(client, "ownresultuser")
    _, other_id = _register(client, "ownresultother")

    challenge_id = _challenge(env, owner_id, "running")
    _participant(
        env, challenge_id, participant_id, 84001,
        status="persisted", population=None, result_verified=0,
    )
    _participant(env, challenge_id, other_id, 84002, status="persisted")

    response = client.get(
        f"/api/challenges/{challenge_id}/results",
        headers=participant_headers,
    )
    assert response.status_code == 200, response.text
    rows = response.json()["participants"]
    assert len(rows) == 1
    assert rows[0]["user_id"] == participant_id
    assert rows[0]["result_status"] == "persisted"
    assert rows[0]["result_json"] == "{}"
    assert rows[0]["result_verified"] is False
    assert "state_json" not in rows[0]

    outsider_headers, _ = _register(client, "ownresultoutsider")
    assert client.get(
        f"/api/challenges/{challenge_id}/results",
        headers=outsider_headers,
    ).status_code == 404


def test_participant_can_view_own_verified_result_before_public_confirmation(f7c_api):
    client, env = f7c_api
    _, owner_id = _register(client, "verifiedowner")
    participant_headers, participant_id = _register(client, "verifieduser")
    _, other_id = _register(client, "verifiedother")

    challenge_id = _challenge(env, owner_id, "completed")
    _participant(
        env, challenge_id, participant_id, 84101,
        status="verified", alias="Private-Verified", population=450,
    )
    _participant(
        env, challenge_id, other_id, 84102,
        status="verified", alias="Other-Verified", population=700,
    )

    own_response = client.get(
        f"/api/challenges/{challenge_id}/results",
        headers=participant_headers,
    )
    assert own_response.status_code == 200, own_response.text
    own_rows = own_response.json()["participants"]
    assert len(own_rows) == 1
    assert own_rows[0]["user_id"] == participant_id
    assert own_rows[0]["result_status"] == "verified"
    assert json.loads(own_rows[0]["result_json"])["total_population"] == 450

    public_response = client.get(f"/api/challenges/{challenge_id}/results")
    assert public_response.status_code == 200, public_response.text
    assert public_response.json()["participants"] == []

    outsider_headers, _ = _register(client, "verifiedoutsider")
    outsider_response = client.get(
        f"/api/challenges/{challenge_id}/results",
        headers=outsider_headers,
    )
    assert outsider_response.status_code == 200, outsider_response.text
    assert outsider_response.json()["participants"] == []


def test_admin_and_organizer_can_view_all_results(f7c_api):
    client, env = f7c_api
    owner_headers, owner_id = _register(client, "allresultowner")
    admin_headers, admin_id = _register(client, "allresultadmin")
    _, participant_id = _register(client, "allresultparticipant")

    with env.Session() as session:
        admin = session.get(UserRow, admin_id)
        admin.is_admin = 1
        session.commit()

    challenge_id = _challenge(env, owner_id, "running")
    _participant(env, challenge_id, participant_id, 85001, status="confirmed")
    _participant(env, challenge_id, admin_id, 85002, status="persisted")

    for headers in (owner_headers, admin_headers):
        response = client.get(
            f"/api/challenges/{challenge_id}/results",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        rows = response.json()["participants"]
        assert len(rows) == 2
        assert all("user_id" in row for row in rows)
        assert all("state_json" not in row for row in rows)