import pytest
from pydantic import ValidationError

from app.schemas import Itinerary, TravelRequest

BASE = {
    "destination": "Paris",
    "start_date": "2026-10-03",
    "end_date": "2026-10-06",
    "budget_level": "medium",
    "travel_style": "balanced",
    "interests": ["museum", "food"],
}


def test_valid_request_parses():
    req = TravelRequest.model_validate(BASE)
    assert req.trip_days == 4
    assert req.language.value == "zh-CN"
    assert req.notes == []


def test_end_before_start_rejected():
    bad = BASE | {"end_date": "2026-10-01"}
    with pytest.raises(ValidationError, match="end_date"):
        TravelRequest.model_validate(bad)


def test_unknown_destination_rejected():
    with pytest.raises(ValidationError, match="只支持"):
        TravelRequest.model_validate(BASE | {"destination": "Berlin"})


def test_empty_interests_rejected():
    with pytest.raises(ValidationError):
        TravelRequest.model_validate(BASE | {"interests": []})


def test_list_fields_are_cleaned_and_deduped():
    req = TravelRequest.model_validate(
        BASE | {"interests": [" museum ", "museum", "", "food"]}
    )
    assert req.interests == ["museum", "food"]


def test_out_of_range_days_adds_note_but_still_valid():
    req = TravelRequest.model_validate(BASE | {"end_date": "2026-10-18"})
    assert req.trip_days == 16
    assert any("建议 4-6 天" in n and "超过 14 天主题里的点不够" in n for n in req.notes)       # 建议天数来自城市配置
    short = TravelRequest.model_validate(BASE | {"end_date": "2026-10-04"})
    assert any("建议 4-6 天" in n and "代表性景点" not in n for n in short.notes)


def test_arrival_date_mismatch_adds_note():
    req = TravelRequest.model_validate(BASE | {"arrival_time": "2026-10-04 09:00"})
    assert any("arrival_time" in n for n in req.notes)


def test_item_type_synonyms_are_normalized():
    from app.schemas import ItemType, ItineraryItem

    def mk(t):
        return ItineraryItem.model_validate(
            {"time": "09:00", "title": "x", "type": t, "location": "y", "duration_minutes": 30}
        ).type

    assert mk("cafe") == ItemType.food
    assert mk("Landmark") == ItemType.sight
    assert mk("gallery") == ItemType.museum
    assert mk("totally-unknown") == ItemType.activity
    assert mk("museum") == ItemType.museum


def test_itinerary_roundtrips():
    data = {
        "trip": {
            "destination": "Paris",
            "date_range": {"start": "2026-10-03", "end": "2026-10-06"},
            "traveler_profile": {
                "style": "balanced",
                "budget": "medium",
                "interests": ["museum"],
            },
        },
        "days": [
            {
                "date": "2026-10-03",
                "theme": "抵达与轻量漫步",
                "items": [
                    {
                        "time": "14:00",
                        "title": "酒店入住",
                        "type": "logistics",
                        "location": "9th arrondissement",
                        "duration_minutes": 60,
                    }
                ],
            }
        ],
    }
    it = Itinerary.model_validate(data)
    assert it.version == "v1"
    assert it.days[0].items[0].booking_required is False
    assert Itinerary.model_validate(it.model_dump(mode="json")) == it
