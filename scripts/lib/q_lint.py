"""Reading the q in a Python file, so a broken query fails at --self-test.

The queries in these scripts are q source held in Python strings.  Nothing
checks them until they reach a kdb server, and when one is wrong the server
answers with two or three characters - `nyi, `type, `par - naming no table, no
column and no line.  A month against the historical server once failed with a
bare `nyi that took a schema diff to place.

None of this needs kdb.  A reserved word used as a variable, unbalanced braces,
a symbol argument compared against char vectors: all of it is visible in the
text, and all of it is the difference between a report of zeros and a report.

What it cannot do is type-check q.  It is a lint, not a parser.
"""

import re

#  The words q will not let you use as a name.  `ss` is the one that actually
#  bit: it is string-search, and a parameter called ss is a PARSE error that
#  surfaces out of pykx as the wonderfully unhelpful `QError: ss`.
Q_RESERVED = frozenset("""
abs acos asin atan avg bin binr by cor cos cov delete dev div do each enlist
exec exit exp from getenv hopen if in insert last like log max min prd select
setenv sin sqrt ss string sum tan update var wavg where within wsum xexp
""".split())

#  Anything that joins or groups inside q.  Not illegal - just not what these
#  reports do: a target is one send and a workorder is a child order, and the
#  chaining, the sums and the counts belong where --self-test can prove them.
#  Keeping q to plain selects also means a failure names one table.
JOIN_WORDS = ("lj", "ij", "uj", "pj", "aj", "ej", "xkey", "0!")


def q_names(src: str) -> set:
    """Every name the q source binds: lambda parameters and locals."""
    out = set()
    for params in re.findall(r"\{\s*\[([^\]]*)\]", src):
        out.update(n.strip() for n in params.split(";") if n.strip())
    for name in re.findall(r"^\s*([a-zA-Z][a-zA-Z0-9_]*)\s*:(?!:)", src, re.M):
        out.add(name)
    return {n for n in out if n}


def reserved_used(src: str) -> list:
    return sorted(q_names(src) & Q_RESERVED)


def balanced(src: str) -> bool:
    return (src.count("{") == src.count("}")
            and src.count("[") == src.count("]")
            and src.count("(") == src.count(")"))


def uncast_symbols(src: str, args) -> list:
    """Symbol arguments used raw against a symbol column.

    PyKX sends a Python str as a CHAR VECTOR.  `sym in syms` with char vectors
    on the right matches NOTHING - and it is not an error, so the report comes
    back empty and a dead query looks exactly like a quiet day.  The fix is
    ``syms:`$syms;`` before use; `like` is exempt, since it wants strings.
    """
    bad = []
    for a in args:
        used_bare = f"in {a}" in src or f"like/: {a}" in src
        cast = f"{a}:`${a};" in src
        like = f"like/: {a}" in src
        if used_bare and not (cast or like):
            bad.append(a)
    return bad


def joins(src: str) -> list:
    """Which join or group-and-flatten words the source uses."""
    out = []
    for w in JOIN_WORDS:
        #  0! is punctuation and runs straight into whatever follows it; the
        #  rest are words, and lj_price must not read as lj
        hit = ("0!" in src if w == "0!" else
               re.search(r"(?<![a-zA-Z0-9_.])" + w + r"(?![a-zA-Z0-9_])", src))
        if hit:
            out.append(w)
    return out


def groups_in_q(src: str) -> list:
    """Lines that aggregate with `by` - the thing these reports do in Python."""
    return [ln.strip() for ln in src.splitlines()
            if " by " in ln and re.search(r"\b(last|first|sum|avg|max|min|"
                                          r"count|wavg)\s", ln)]


def self_test() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("q_lint --self-test\n\nnames a query binds")
    check("a lambda's parameters", q_names("{[hist;d;sfx] 1}"),
          {"hist", "d", "sfx"})
    check("and its locals", q_names("{[d]\n  t:1;\n  w:2;\n  t}"),
          {"d", "t", "w"})
    check("a comparison is not an assignment", q_names("{[d] a::1; b=2}"),
          {"d"})

    print("\nreserved words")
    check("ss is the one that bit - it is string search",
          reserved_used("{[d;ss] ss:1}"), ["ss"])
    check("and so would last, or in",
          reserved_used("{[d]\n last:1;\n in:2}"), ["in", "last"])
    check("a clean query has none",
          reserved_used("{[hist;d]\n t:1;\n t}"), [])
    #  only NAMES are looked at, so the query is free to use the words as words
    check("using them as functions is what they are for",
          reserved_used("{[d]\n t:select last px by sym from z "
                        "where date in d;\n t}"), [])

    print("\nbrackets")
    check("balanced", balanced("{[d] (1;2)[0]}"), True)
    check("a missing brace", balanced("{[d] 1"), False)
    check("a missing paren", balanced("{[d] (1}"), False)

    print("\nsymbol arguments")
    check("`in` against a char vector matches nothing, silently",
          uncast_symbols("{[syms] select from t where sym in syms}", ["syms"]),
          ["syms"])
    check("cast first and it is fine",
          uncast_symbols("{[syms] syms:`$syms; select from t where sym in "
                         "syms}", ["syms"]), [])
    check("like wants strings, so it is exempt",
          uncast_symbols("{[sfx] select from t where any sym like/: sfx}",
                         ["sfx"]), [])
    check("an argument that is never used that way is not flagged",
          uncast_symbols("{[d] select from t where date=d}", ["d"]), [])

    print("\njoins and grouping")
    check("lj", joins("t:t lj `a xkey x"), ["lj", "xkey"])
    check("and 0!", joins("0!select a by b from x"), ["0!"])
    check("a plain select has none", joins("select a,b from t where date=d"),
          [])
    check("a column CALLED lj_price is not a join",
          joins("select lj_price from t"), [])
    check("aggregating by a key is grouping",
          groups_in_q("x:select refpx:last adjclose by date,id from y"),
          ["x:select refpx:last adjclose by date,id from y"])
    check("`by` with no aggregate is not",
          groups_in_q("select from t where date=d"), [])

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
