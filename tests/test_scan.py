from sn2docx.latex.scan import (
    find_env,
    latex_to_plain,
    parse_command_args,
    read_group,
    remove_command,
    split_top_level,
    strip_comments,
    strip_top_level,
)


def test_read_group_nested_and_escaped():
    s = r"{a{b}\}c}rest"
    content, end = read_group(s, 0)
    assert content == r"a{b}\}c"
    assert s[end:] == "rest"


def test_parse_command_args_star_optional_mandatory():
    s = r"\section*[short]{Long title}after"
    args, end = parse_command_args(s, len(r"\section"), "som")
    assert args == ["*", "short", "Long title"]
    assert s[end:] == "after"


def test_strip_comments_joins_lines_and_keeps_verbatim():
    src = "a% comment\n  b\n%whole line\nc 100\\% \\verb|%x|\n\\begin{verbatim}\n% kept\n\\end{verbatim}\n"
    out = strip_comments(src)
    assert out.startswith("ab\nc 100\\% \\verb|%x|")
    assert "% kept" in out
    assert "whole line" not in out


def test_find_env_handles_nesting():
    s = r"\begin{a}x\begin{a}y\end{a}z\end{a}tail"
    env = find_env(s, "a")
    assert env.body(s) == r"x\begin{a}y\end{a}z"
    assert s[env.end :] == "tail"


def test_split_top_level_rows_ignore_nested_envs():
    body = r"a &= b \\ c &= \begin{cases} 1 \\ 2 \end{cases} \\[2pt] d"
    rows = split_top_level(body)
    assert [r.strip() for r in rows] == ["a &= b", r"c &= \begin{cases} 1 \\ 2 \end{cases}", "d"]


def test_strip_top_level_ampersands_only():
    assert strip_top_level(r"a &= \begin{matrix}1&2\end{matrix}") == r"a  = \begin{matrix}1&2\end{matrix}"


def test_remove_command_keep_argument():
    assert remove_command(r"x \textbf{bold} y", "textbf", "m", keep=0) == "x bold y"


def test_latex_to_plain_symbols():
    assert latex_to_plain(r"$\star$") == "\u22c6"
    assert latex_to_plain(r"\fnm{Ada} \sur{Lovelace}") == "Ada Lovelace"
