from datetime import date

from conftest import add_entry

from journal_analyzer import extractor, ingester, store


def test_a_summary_written_from_outside_is_caught_wherever_it_says_so():
    assert extractor.is_detached("The author spent the day hiking.")
    assert extractor.is_detached("Spent the day hiking, which the writer's sister suggested.")
    assert not extractor.is_detached("Spent the day hiking with my sister.")
    assert not extractor.is_detached("Read a book by my favourite author.")


def _revoiced(text):
    return extractor._Revoiced(summary=text)


def test_old_summaries_are_rewritten_in_the_writers_voice(conn, config, fake_client):
    outside = add_entry(conn, date(2024, 1, 1), summary="The author felt drained after work.")
    mine = add_entry(conn, date(2024, 1, 2), summary="Walked by the river with Sam.", path="/vault/b.md")
    asked = []

    def revoice(model, messages):
        asked.append(messages[-1]["content"])
        return _revoiced("Felt drained after work.")

    fake_client.structured["_Revoiced"] = revoice
    ingester._refresh_voice(config, conn, fake_client)
    summaries = {r["id"]: r["summary"] for r in store.entries_with_summaries(conn)}
    assert summaries == {outside: "Felt drained after work.", mine: "Walked by the river with Sam."}
    # only the one written from outside went to the model
    assert asked == ["The author felt drained after work."]
    ingester._refresh_voice(config, conn, fake_client)
    assert len(asked) == 1


def test_a_rewrite_that_is_still_from_outside_keeps_the_old_summary(conn, config, fake_client):
    entry = add_entry(conn, date(2024, 1, 1), summary="The author felt drained.")
    fake_client.structured["_Revoiced"] = _revoiced("The writer felt drained.")
    ingester._refresh_voice(config, conn, fake_client)
    [row] = [r for r in store.entries_with_summaries(conn) if r["id"] == entry]
    assert row["summary"] == "The author felt drained."
