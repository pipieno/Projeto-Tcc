import pytest

from backend.places import detect_place_category


@pytest.mark.parametrize("message,expected", [
    ("me manda as farmacias", "pharmacy"),
    ("me manda as farmácias", "pharmacy"),
    ("Você pode me mandar as farmácias?", "pharmacy"),
    ("agora me passa as farmácias por favor", "pharmacy"),
    ("então me envie as farmácias", "pharmacy"),
    ("consegue me listar os restaurantes?", "food"),
    ("me mostra as padarias", "bakery"),
    ("me recomenda uma cafeteria", "cafe"),
    ("traga os mercados", "market"),
    ("me indica os bancos", "bank"),
    ("e as farmácias?", "pharmacy"),
    ("farmácias", "pharmacy"),
])
def test_requests_for_places_are_routed_to_map(message, expected):
    assert detect_place_category(message).key == expected


@pytest.mark.parametrize("message", [
    "A AMOBEN serve café nas reuniões?",
    "A AMOBEN manda remédios aos moradores?",
    "A associação envia remédios para os moradores?",
    "Como a AMOBEN ajuda as escolas?",
    "Você gosta de café?",
    "me manda os contatos da AMOBEN",
])
def test_other_requests_stay_with_model(message):
    assert detect_place_category(message) is None
