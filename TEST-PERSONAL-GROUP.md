# Kokabiel Personal & Group Update — Live Test Checklist

Run these tests in the real film/TV/documentary group after restarting Kokabiel.

## 1. Card classification

Search one example of each available type:

```text
/movie They Live
/tv Silo
/tv Chernobyl
/documentary The Act of Killing
/documentary Planet Earth III
```

Confirm that cards identify the production as Film, Documentary Film, TV Series, Limited Series, Documentary Series, or Documentary Limited Series and that series cards show seasons/episodes rather than treating the production like one movie.

## 2. Personal watchlist

1. Member A taps **My Watchlist** on a title.
2. Run `/mywatchlist` as Member A and confirm the title appears.
3. Run `/mywatchlist` as Member B and confirm it does not appear unless Member B also added it.
4. Run `/watchlist` and confirm the title appears once with the correct member count.

## 3. Movie watched status

1. Tap **Mark watched** on a movie or documentary film.
2. Run `/watched` and confirm it appears only for that member.
3. Tap **Mark watched** again and confirm the personal watched status is removed.

## 4. Series progress

1. Tap **My Progress** on a TV series, miniseries, or documentary series.
2. Confirm another member cannot operate that personal menu.
3. Open **Choose a season**.
4. Toggle one released episode.
5. Confirm `/myprogress` shows the title and episode count.
6. Mark a whole season watched.
7. Use **Mark all released watched**.
8. Confirm an ongoing show becomes **Caught up** and an ended/miniseries title becomes **Completed**.
9. Clear one season and then clear all progress.

## 5. Ratings

1. Member A taps **Rate** and selects a score.
2. Member B confirms they cannot operate Member A's rating menu.
3. Member B rates the same title.
4. Search the title again and confirm the group average reflects both ratings.

## 6. Group suggestions

1. Member A taps **Suggest to Group**.
2. Member B suggests the same title.
3. Run `/suggestions` and confirm the title appears once with two members.
4. Run `/mysuggestions` for both members.
5. Have Member A tap **Suggest to Group** again and confirm only Member A's suggestion is removed.

## 7. Migration check

Confirm the title previously stored in the old group watchlist was assigned to the member who originally added it rather than being marked for the entire group.
