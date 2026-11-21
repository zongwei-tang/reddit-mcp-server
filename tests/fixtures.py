"""Response fixtures.

Archive shapes follow real Arctic Shift responses captured 2026-11-20 (trimmed to
the keys the parser reads plus a few distractors). Live shapes follow reddit.com's
standard Listing JSON.
"""

from __future__ import annotations

NOW = 1795240000.0  # fixed "now" for tests; about 2026-11-20


def archive_post(id="1y1f78s", created=NOW - 5 * 86400, **over):
    post = {
        "all_awardings": [],
        "author": "someone",
        "author_fullname": "t2_abc",
        "created_utc": created,
        "domain": "self.Python",
        "id": id,
        "is_self": True,
        "link_flair_text": "Discussion",
        "locked": False,
        "name": f"t3_{id}",
        "num_comments": 5,
        "over_18": False,
        "permalink": f"/r/Python/comments/{id}/some_title/",
        "removed_by_category": None,
        "retrieved_on": created + 4,
        "score": 42,
        "selftext": "Body text of the post.",
        "spoiler": False,
        "stickied": False,
        "subreddit": "Python",
        "title": "Some title",
        "upvote_ratio": 0.9,
        "url": f"https://www.reddit.com/r/Python/comments/{id}/some_title/",
    }
    post.update(over)
    return post


def t1(id, parent, depth, body, score=1, created=NOW - 86400 * 3, replies=""):
    return {
        "kind": "t1",
        "data": {
            "author": f"user_{id[:4]}",
            "body": body,
            "created_utc": created,
            "depth": depth,
            "id": id,
            "link_id": "t3_1y1f78s",
            "name": f"t1_{id}",
            "parent_id": parent,
            "score": score,
            "replies": replies,
            "stickied": False,
        },
    }


def listing(children):
    return {"kind": "Listing", "data": {"dist": 0, "children": children}}


def more(id, parent, depth, children):
    return {
        "kind": "more",
        "data": {
            "count": len(children),
            "id": id,
            "name": f"t1_{id}",
            "parent_id": parent,
            "depth": depth,
            "children": children,
        },
    }


# Shape of /api/comments/tree?link_id=t3_1y1f78s&limit=4&start_depth=1&start_breadth=1
ARCHIVE_TREE = {
    "data": [
        t1(
            "19sxtv3lgpatd",
            "t3_1y1f78s",
            0,
            "Why do you not care about package vulnerabilities?",
            score=11,
            replies=listing(
                [
                    t1(
                        "19sxu4okgmlmq",
                        "t1_19sxtv3lgpatd",
                        1,
                        "Work for a big company.",
                        score=2,
                        replies=listing(
                            [t1("19sz0000deep1", "t1_19sxu4okgmlmq", 2, "deep reply", score=5)]
                        ),
                    ),
                    more("19sxv1x9ye1vm", "t1_19sxtv3lgpatd", 1, ["19sxv1x9ye1vm"]),
                ]
            ),
        ),
        t1("19sxxue068ver", "t3_1y1f78s", 0, "Terrible package management.", score=0),
        t1("19sxzqc9u6fpg", "t3_1y1f78s", 0, "I agree with the op.", score=1),
    ]
}


def live_post_listing(id="abc123"):
    return {
        "kind": "Listing",
        "data": {
            "after": None,
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        **archive_post(id=id, created=NOW - 3600),
                        "subreddit": "pics",
                        "permalink": f"/r/pics/comments/{id}/x/",
                    },
                }
            ],
        },
    }


def live_comments_listing():
    return {
        "kind": "Listing",
        "data": {
            "after": None,
            "children": [
                t1("c1", "t3_abc123", 0, "top comment", score=10,
                   replies=listing([t1("c2", "t1_c1", 1, "reply", score=3)])),
                {
                    "kind": "more",
                    "data": {"count": 0, "id": "_", "name": "t1__", "parent_id": "t1_c1",
                             "depth": 1, "children": []},
                },
            ],
        },
    }
