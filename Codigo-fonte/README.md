# Sponte XML -> Asaas

MVP local para o financeiro carregar o XML mensal exportado do Sponte, revisar as parcelas e enviar manualmente os registros selecionados para o Asaas.

## O que este MVP faz

- Le o XML mensal do Sponte a partir de um caminho local.
- Filtra apenas `Situacao = Pendente` e `FormaCobranca = Boleto Automatizado`.
- Mostra painel visual com aluno, responsavel, pagador, valores, vencimento e desconto.
- Permite selecionar manualmente por checkbox.
- Faz uma etapa de revisao antes do envio, com ajuste manual de CPF, email e telefone do pagador.
- No Asaas, sincroniza por `externalReference` derivado de `NumeroMatricula + DataVencimento`.
- Se o registro ja existir, atualiza.
- Se nao existir, cria customer e cobranca.
- Se um registro antes sincronizado sumir do XML atual, ele aparece na secao de ausentes para exclusao manual no Asaas.

## Decisao desta adaptacao

Este MVP foi adaptado para **cobrancas por parcela** no Asaas, e nao para assinatura recorrente.

Motivo:
- o XML ja vem mensal, parcela a parcela
- a chave unica pedida e `NumeroMatricula + DataVencimento`
- o financeiro quer controle manual do que entra a cada rodada

Isso encaixa melhor em `payments` do Asaas do que em `subscriptions`.

## Regras implementadas

- Pagador:
  - usa responsavel financeiro quando houver dados do responsavel
  - senao usa o aluno (`Sacado`)
- Valor:
  - prioridade `ValorLiquido`
  - fallback `ValorComDesconto`
  - fallback `Valor`
- Desconto:
  - se o percentual em `Bolsa` for confiavel frente a `Valor` x `Valor final`, envia `value = valor cheio` + `discount = percentual`
  - se nao for confiavel, envia apenas `value = valor final`

## Como rodar

Opção 1:

```powershell
python app.py
```

Opção 2:

```text
duplo clique em abrir_painel_sponte.bat
```

Ao iniciar, o navegador deve abrir sozinho. Se nao abrir, acesse:

```text
http://127.0.0.1:8765
```

## Como virar EXE

Para gerar a versao Windows:

```text
duplo clique em build_exe.bat
```

Quando terminar, o executavel fica em:

```text
dist\PainelSponteAsaas\PainelSponteAsaas.exe
```

Observacoes:

- O build usa `PyInstaller`.
- O `.exe` gerado abre sem console e tenta abrir o navegador sozinho.
- Os arquivos `sync_state.json`, `current_xml_report.json` e a pasta `uploads` ficam ao lado do executavel quando ele estiver rodando empacotado.
- Para distribuir para uma colaboradora, envie a pasta inteira `dist\PainelSponteAsaas`, nao so o `.exe`.

## Arquivos gerados

- `current_xml_report.json`: snapshot do XML carregado e do painel atual
- `sync_state.json`: estado local dos registros sincronizados com o Asaas

## Observacoes

- O token do Asaas nao fica salvo em arquivo por este MVP.
- Voce pode carregar o XML de 2 jeitos:
  - informando o caminho local
  - anexando o arquivo direto pela tela
- Se algum registro vier sem CPF, email ou telefone suficientes para o Asaas, a tela de revisao deixa voce corrigir antes de enviar.
