"""Exceções do pg_atlas. As mensagens nunca carregam host, usuário ou senha."""


class ErroAtlas(Exception):
    """Base de todos os erros esperados; a CLI mostra a mensagem e sai com `codigo_saida`."""

    codigo_saida = 1


class ErroConfig(ErroAtlas):
    codigo_saida = 2


class ErroCredenciais(ErroAtlas):
    codigo_saida = 2


class ErroConexao(ErroAtlas):
    codigo_saida = 3


class ErroTravaOcupada(ErroAtlas):
    codigo_saida = 3


class ErroExecucao(ErroAtlas):
    """Falha ao executar um SQL aceito pelo guarda. `sqlstate` é só o código do servidor, nunca texto dele."""

    codigo_saida = 4

    def __init__(self, mensagem: str, sqlstate: str | None = None) -> None:
        super().__init__(mensagem)
        self.sqlstate = sqlstate


class SqlRejeitado(ErroAtlas):
    """O guarda de SQL recusou o comando; ele não chegou ao banco."""

    codigo_saida = 5
