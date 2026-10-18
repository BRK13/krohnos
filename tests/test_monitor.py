import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import monitor

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)

# /proc/stat: campos user nice system idle iowait irq softirq steal guest guest_nice.
# Entre as duas leituras: total agregado +400 (ocioso +300 = idle 280 + iowait 20) => 25,0 %; cpu0 +100 (ocioso +50) => 50,0 %;
# cpu1 +300 (ocioso +250) => 16,7 %. O guest (99 na linha agregada e na cpu0) já está em user e não entra na conta.
STAT1 = """cpu  1000 0 500 8000 500 0 0 0 0 0
cpu0 400 0 200 4000 200 0 0 0 0 0
cpu1 600 0 300 4000 300 0 0 0 0 0
intr 1 2 3
"""
STAT2 = """cpu  1050 0 550 8280 520 0 0 0 99 0
cpu0 430 0 220 4040 210 0 0 0 99 0
cpu1 620 0 330 4240 310 0 0 0 0 0
intr 4 5 6
"""
# diskstats: lido = campo 6 (setores), gravado = campo 10. Em 10 s, sda lê +1.953.125 setores (1e9 B = 100 MB/s) e grava
# +390.625 setores (2e8 B = 20 MB/s). Partições, loop, dm e o resto mexem muito e não podem entrar.
DISK1 = """   8       0 sda 100 0 1000000 5 100 0 2000000 5 0 5 10
   8       1 sda1 90 0 900000 5 90 0 1900000 5 0 5 10
   7       0 loop0 1 0 77777 0 0 0 0 0 0 0 0
 253       0 dm-0 1 0 55555 0 0 0 66666 0 0 0 0
 259       0 nvme0n1 1 0 0 0 1 0 0 0 0 0 0
 259       1 nvme0n1p1 1 0 5 0 1 0 5 0 0 0 0
"""
DISK2 = """   8       0 sda 200 0 2953125 5 200 0 2390625 5 0 5 10
   8       1 sda1 190 0 1900000 5 190 0 2900000 5 0 5 10
   7       0 loop0 1 0 99999 0 0 0 0 0 0 0 0
 253       0 dm-0 1 0 99999 0 0 0 99999 0 0 0 0
 259       0 nvme0n1 1 0 0 0 1 0 0 0 0 0 0
 259       1 nvme0n1p1 1 0 999 0 1 0 999 0 0 0 0
"""
# rede: em 10 s, eth0 rx +20.000.000 B => 2,00 MB/s; tx +5.000.000 B => 0,50 MB/s. As demais interfaces andam muito e são ignoradas.
NET1 = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 1000 1 0 0 0 0 0 0 1000 1 0 0 0 0 0 0
  eth0: 100000000 10 0 0 0 0 0 0 50000000 10 0 0 0 0 0 0
vethabc: 5 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0
docker0: 5 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0
br-1234: 5 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0
"""
NET2 = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 9999999 1 0 0 0 0 0 0 9999999 1 0 0 0 0 0 0
  eth0: 120000000 10 0 0 0 0 0 0 55000000 10 0 0 0 0 0 0
vethabc: 9999999 1 0 0 0 0 0 0 9999999 1 0 0 0 0 0 0
docker0: 9999999 1 0 0 0 0 0 0 9999999 1 0 0 0 0 0 0
br-1234: 9999999 1 0 0 0 0 0 0 9999999 1 0 0 0 0 0 0
"""
MEMINFO = """MemTotal:       16777216 kB
MemFree:         1000000 kB
MemAvailable:   12582912 kB
SwapTotal:       4194304 kB
SwapFree:        3145728 kB
"""


def montar_proc(raiz, stat, disk, net, mem=MEMINFO, carga="0.96 1.04 1.27 2/300 12345\n"):
    p = Path(raiz)
    (p / "net").mkdir(parents=True, exist_ok=True)
    (p / "stat").write_text(stat)
    (p / "diskstats").write_text(disk)
    (p / "net" / "dev").write_text(net)
    (p / "meminfo").write_text(mem)
    (p / "loadavg").write_text(carga)
    return str(p)


def montar_pid(raiz, pid, nome, ticks, rss_paginas, inicio="100", uid=0):
    d = Path(raiz) / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    resto = ["S", "1"] + ["0"] * 9 + [str(ticks), "0"] + ["0"] * 6 + [inicio] + ["0"] * 5
    (d / "stat").write_text(f"{pid} ({nome} x) " + " ".join(resto) + "\n")
    (d / "statm").write_text(f"1000 {rss_paginas} 10 0 0 0 0\n")
    (d / "comm").write_text(nome + "\n")
    (d / "status").write_text(f"Name:\t{nome}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")


class ParseTest(unittest.TestCase):
    def test_stat_ocioso_inclui_iowait_e_ignora_guest(self):
        r = monitor.parse_stat(STAT2)
        self.assertEqual(r["cpu"], (8280 + 520, 1050 + 550 + 8280 + 520))
        self.assertEqual(set(r), {"cpu", "cpu0", "cpu1"})

    def test_diskstats_so_discos_inteiros(self):
        self.assertEqual(monitor.parse_diskstats(DISK1), (1000000, 2000000))

    def test_diskstats_com_conjunto_do_sys_block(self):
        self.assertEqual(monitor.parse_diskstats(DISK1, {"sda"}), (1000000, 2000000))
        self.assertEqual(monitor.parse_diskstats(DISK1, {"sda1"}), (900000, 1900000))
        self.assertFalse(monitor.disco_inteiro("loop0", {"loop0"}))

    def test_nomes_de_disco(self):
        for n in ("sda", "vdb", "xvda", "nvme0n1", "hdc", "mmcblk0"):
            self.assertTrue(monitor.disco_inteiro(n), n)
        for n in ("sda1", "vda2", "nvme0n1p1", "loop3", "ram0", "zram0", "dm-0", "sr0", "fd0", "md0", "mmcblk0p1"):
            self.assertFalse(monitor.disco_inteiro(n), n)

    def test_rede_filtra_interfaces(self):
        self.assertEqual(monitor.parse_netdev(NET1), (100000000, 50000000))
        for n in ("lo", "veth1", "docker0", "br-ab", "virbr0", "vxlan0", "cali1", "flannel.1", "tun0", "tap0", "cni0", "kube-ipvs0"):
            self.assertFalse(monitor.interface_fisica(n), n)
        for n in ("eth0", "ens3", "enp0s3", "wlan0", "bond0"):
            self.assertTrue(monitor.interface_fisica(n), n)

    def test_meminfo_e_loadavg(self):
        self.assertEqual(monitor.parse_meminfo(MEMINFO)["MemAvailable"], 12582912)
        self.assertEqual(monitor.parse_loadavg("0.96 1.04 1.27 2/300 1"), [0.96, 1.04, 1.27])


class PontoTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def leituras(self, stat2=STAT2, disk2=DISK2, net2=NET2, dt_s=10):
        a = monitor.leitura(montar_proc(self.tmp / "p1", STAT1, DISK1, NET1), str(self.tmp), "/nao-existe", agora=T0)
        b = monitor.leitura(montar_proc(self.tmp / "p2", stat2, disk2, net2), str(self.tmp), "/nao-existe",
                            agora=T0 + dt.timedelta(seconds=dt_s))
        return a, b

    def test_cpu_io_e_rede_de_duas_leituras(self):
        p = monitor.ponto(*self.leituras(), docker_gb=120.4)
        self.assertEqual(p["t"], "2026-10-10T12:00:10+00:00")
        self.assertEqual(p["cpu"], 25.0)
        self.assertEqual(p["nucleos"], [50.0, 16.7])
        self.assertEqual(p["n_cpu"], 2)
        # em 10 s: 1.953.125 setores * 512 B = 1e9 B => 100,00 MB/s lidos; 390.625 * 512 = 2e8 B => 20,00 MB/s gravados
        self.assertEqual(p["io_ler_mbs"], 100.0)
        self.assertEqual(p["io_gravar_mbs"], 20.0)
        self.assertEqual(p["rede_rx_mbs"], 2.0)
        self.assertEqual(p["rede_tx_mbs"], 0.5)
        self.assertEqual(p["carga"], [0.96, 1.04, 1.27])
        self.assertEqual(p["docker_gb"], 120.4)

    def test_nucleo_0_exato(self):
        # cpu0: total +100 (user 30 + system 20 + idle 40 + iowait 10), ocioso +50 => 50,0 %
        p = monitor.ponto(*self.leituras())
        self.assertEqual(p["nucleos"][0], 50.0)
        # cpu1: total +(20+30+240+10)=300, ocioso +250 => 16,7 %
        self.assertEqual(p["nucleos"][1], 16.7)

    def test_memoria_e_swap(self):
        p = monitor.ponto(*self.leituras())
        self.assertEqual((p["mem_total_gb"], p["mem_usado_gb"], p["mem_pct"]), (16.0, 4.0, 25.0))
        self.assertEqual((p["swap_total_gb"], p["swap_usado_gb"]), (4.0, 1.0))

    def test_primeira_leitura_sem_anterior(self):
        _, b = self.leituras()
        p = monitor.ponto(None, b)
        for k in ("cpu", "io_ler_mbs", "io_gravar_mbs", "rede_rx_mbs", "rede_tx_mbs"):
            self.assertIsNone(p[k], k)
        self.assertEqual(p["nucleos"], [None, None])
        self.assertEqual(p["mem_total_gb"], 16.0)

    def test_contador_que_voltou_vira_null(self):
        a, b = self.leituras(net2=NET1.replace("100000000", "10"), disk2=DISK1.replace("sda 100 0 1000000", "sda 100 0 5"))
        p = monitor.ponto(a, b)
        self.assertIsNone(p["rede_rx_mbs"])
        self.assertEqual(p["rede_tx_mbs"], 0.0)
        self.assertIsNone(p["io_ler_mbs"])
        self.assertEqual(p["io_gravar_mbs"], 0.0)
        # contador de CPU que voltou
        a2, b2 = self.leituras(stat2=STAT1.replace("cpu  1000", "cpu  10"))
        self.assertIsNone(monitor.ponto(a2, b2)["cpu"])

    def test_relogio_parado_ou_para_tras(self):
        for d in (0, -5):
            a, b = self.leituras(dt_s=d)
            p = monitor.ponto(a, b)
            self.assertIsNone(p["cpu"])
            self.assertIsNone(p["rede_rx_mbs"])
            self.assertIsNone(p["io_ler_mbs"])

    def test_falha_de_leitura_vira_null(self):
        b = monitor.leitura(str(self.tmp / "nao-existe"), str(self.tmp), agora=T0)
        p = monitor.ponto(None, b)
        self.assertIsNone(p["mem_pct"])
        self.assertIsNone(p["carga"])
        self.assertIsNone(p["n_cpu"])

    def test_disco_do_statvfs(self):
        _, b = self.leituras()
        p = monitor.ponto(None, b)
        self.assertIsNotNone(p["disco_total_gb"])
        self.assertTrue(0 <= p["disco_pct"] <= 100)


class ProcessosTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.proc = Path(tmp.name)

    def test_top_cpu_e_mem(self):
        montar_pid(self.proc, 10, "node", 1000, 25600)   # 100 MiB com página de 4096
        montar_pid(self.proc, 11, "python3", 500, 1000)
        montar_pid(self.proc, 12, "novo", 50, 100, inicio="777")
        (self.proc / "self").mkdir()
        (self.proc / "999").mkdir()  # sumiu no meio: sem arquivos
        antes = monitor.processos_lidos(str(self.proc))
        self.assertEqual(set(antes), {"10:100", "11:100", "12:777"})
        ant = {k: v["ticks"] for k, v in antes.items()}
        # 10 s depois: pid 10 gastou +500 ticks, pid 11 +100; pid 12 foi reciclado (outro starttime) e não conta
        montar_pid(self.proc, 10, "node", 1500, 25600)
        montar_pid(self.proc, 11, "python3", 600, 1000)
        montar_pid(self.proc, 12, "outro", 99999, 100, inicio="888")
        atuais = monitor.processos_lidos(str(self.proc))
        with mock.patch("os.sysconf", side_effect=lambda n: 4096 if n == "SC_PAGE_SIZE" else 100):
            top = monitor.processos_top(atuais, ant, 10, str(self.proc), clk_tck=100)
        self.assertEqual([(x["pid"], x["cpu"]) for x in top["cpu"]], [(10, 50.0), (11, 10.0)])
        self.assertEqual(top["cpu"][0]["nome"], "node")
        self.assertEqual(top["mem"][0]["pid"], 10)
        self.assertEqual(len(top["mem"]), 3)
        self.assertTrue(top["mem"][0]["usuario"])

    def test_sem_anterior_nao_tem_top_de_cpu(self):
        montar_pid(self.proc, 10, "node", 1000, 100)
        top = monitor.processos_top(monitor.processos_lidos(str(self.proc)), None, None, str(self.proc))
        self.assertEqual(top["cpu"], [])
        self.assertEqual(len(top["mem"]), 1)


DOCKER_STATS = "\n".join(json.dumps(x) for x in [
    {"Name": "clientei-psi-site-mmtiik.1.xtik74r5bsxt2o5v7v40zm7v3", "CPUPerc": "2.25%", "MemUsage": "132.8MiB / 31.34GiB", "MemPerc": "0.41%"},
    {"Name": "fsite05-db", "CPUPerc": "10.50%", "MemUsage": "1GiB / 31.34GiB", "MemPerc": "3.19%"},
    {"Name": "pequeno", "CPUPerc": "0.00%", "MemUsage": "512kB / 31.34GiB", "MemPerc": "0.00%"},
]) + "\nlixo que não é json"

DF_JSON = "\n".join(json.dumps(x) for x in [
    {"Type": "Images", "Size": "12.3GB", "Reclaimable": "1GB (8%)"},
    {"Type": "Containers", "Size": "500MB"},
    {"Type": "Local Volumes", "Size": "2.2GB"},
    {"Type": "Build Cache", "Size": "5GB"},
])


class DockerTest(unittest.TestCase):
    def test_tamanhos(self):
        for txt, esperado in (("0B", 0), ("1kB", 1000), ("1KiB", 1024), ("131.8MiB", 131.8 * 2 ** 20), ("12.3GB", 12.3e9),
                              ("31.34GiB", 31.34 * 2 ** 30), ("5MB", 5e6)):
            self.assertAlmostEqual(monitor.tamanho_bytes(txt), esperado, msg=txt)
        self.assertIsNone(monitor.tamanho_bytes("--"))

    def test_parse_stats(self):
        r = monitor.parse_docker_stats(DOCKER_STATS)
        self.assertEqual([x["nome"] for x in r], ["fsite05-db", "clientei-psi-site-mmtiik", "pequeno"])
        self.assertEqual(r[0], {"nome": "fsite05-db", "cpu": 10.5, "mem_mb": 1024.0, "mem_pct": 3.19})
        self.assertEqual(r[1]["mem_mb"], 132.8)
        self.assertEqual(r[2]["mem_mb"], 0.5)

    def test_stats_com_subprocess_mockado(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=DOCKER_STATS, stderr="")
            self.assertEqual(len(monitor.docker_stats()), 3)
            self.assertEqual(run.call_args.args[0][:3], ["docker", "stats", "--no-stream"])
            self.assertEqual(run.call_args.kwargs["timeout"], 15)
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="erro")
            self.assertIsNone(monitor.docker_stats())
            run.side_effect = FileNotFoundError("docker")
            self.assertIsNone(monitor.docker_stats())

    def test_parse_df(self):
        r = monitor.parse_docker_df(DF_JSON, T0)
        self.assertEqual(r, {"em": "2026-10-10T12:00:00+00:00", "imagens_gb": 12.3, "containers_gb": 0.5, "volumes_gb": 2.2,
                             "cache_gb": 5.0, "total_gb": 20.0, "aprox": False})
        self.assertIsNone(monitor.parse_docker_df("", T0))

    def test_df_cai_no_plano_b_e_deduplica_por_id(self):
        def run(cmd, **kw):
            if cmd[:3] == ["docker", "system", "df"]:
                return mock.Mock(returncode=1, stdout="", stderr="rw layer snapshot not found")
            return mock.Mock(returncode=0, stdout="aaa 2.5GB\nbbb 500MB\nbbb 500MB\nccc 1GB\n", stderr="")
        with mock.patch("subprocess.run", side_effect=run):
            r = monitor.docker_df(T0)
        self.assertEqual((r["imagens_gb"], r["total_gb"], r["aprox"]), (4.0, 4.0, True))
        self.assertIsNone(r["volumes_gb"])

    def test_df_tudo_falhou(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stdout="", stderr="x")):
            self.assertIsNone(monitor.docker_df(T0))


class ColetaTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.dados = str(self.tmp / "dados")
        self.docker = mock.patch.multiple(monitor, docker_stats=lambda: [{"nome": "x", "cpu": 1.0, "mem_mb": 1.0, "mem_pct": 0.1}],
                                          docker_df=lambda agora=None: {"em": "e", "total_gb": 20.0, "aprox": False})
        self.docker.start()
        self.addCleanup(self.docker.stop)

    def coletar(self, minutos, stat, disk, net):
        proc = montar_proc(self.tmp / "proc", stat, disk, net)
        return monitor.coletar(self.dados, T0 + dt.timedelta(seconds=minutos), proc, str(self.tmp))

    def test_duas_coletas_gravam_arquivos_e_calculam_delta(self):
        r1 = self.coletar(0, STAT1, DISK1, NET1)
        self.assertIsNone(r1["ponto"]["cpu"])
        r2 = self.coletar(10, STAT2, DISK2, NET2)
        self.assertEqual(r2["ponto"]["cpu"], 25.0)
        self.assertEqual(r2["ponto"]["rede_rx_mbs"], 2.0)
        self.assertEqual(r2["ponto"]["docker_gb"], 20.0)
        d = Path(self.dados) / "monitor"
        self.assertEqual(len((d / "2026-10-10.jsonl").read_text().splitlines()), 2)
        self.assertEqual(json.loads((d / "atual.json").read_text())["ponto"]["cpu"], 25.0)
        self.assertEqual(monitor.atual(self.dados)["containers"][0]["nome"], "x")
        self.assertTrue((d / "estado.json").exists())
        self.assertEqual([f.name for f in d.glob(".*tmp")], [])

    def test_atual_sem_coleta_e_none(self):
        self.assertIsNone(monitor.atual(self.dados))

    def test_df_so_de_10_em_10_minutos(self):
        chamadas = []
        with mock.patch.object(monitor, "docker_df", side_effect=lambda agora=None: chamadas.append(1) or {"total_gb": 1.0}):
            self.coletar(0, STAT1, DISK1, NET1)
            self.coletar(60, STAT2, DISK2, NET2)
            self.assertEqual(len(chamadas), 1)
            self.coletar(700, STAT2, DISK2, NET2)
            self.assertEqual(len(chamadas), 2)

    def test_df_que_falha_mantem_o_ultimo_bom(self):
        with mock.patch.object(monitor, "docker_df", side_effect=[{"total_gb": 7.0}, None]):
            self.coletar(0, STAT1, DISK1, NET1)
            r = self.coletar(700, STAT2, DISK2, NET2)
        self.assertEqual(r["ponto"]["docker_gb"], 7.0)

    def test_docker_fora_vira_null(self):
        with mock.patch.object(monitor, "docker_stats", return_value=None), \
                mock.patch.object(monitor, "docker_df", return_value=None):
            r = self.coletar(0, STAT1, DISK1, NET1)
        self.assertIsNone(r["containers"])
        self.assertIsNone(r["docker"])
        self.assertIsNone(r["ponto"]["docker_gb"])

    def test_retencao_de_14_dias(self):
        d = Path(self.dados) / "monitor"
        d.mkdir(parents=True)
        for dia in ("2026-09-25", "2026-09-26", "2026-10-01"):
            (d / f"{dia}.jsonl").write_text("{}\n")
        (d / "outro.jsonl").write_text("x")
        self.coletar(0, STAT1, DISK1, NET1)
        # hoje 10/10: o corte é 26/09 (14 dias); 25/09 sai, 26/09 fica
        self.assertEqual(sorted(f.name for f in d.glob("*.jsonl")), ["2026-09-26.jsonl", "2026-10-01.jsonl", "2026-10-10.jsonl", "outro.jsonl"])

    def test_virada_do_dia_utc(self):
        self.coletar(0, STAT1, DISK1, NET1)
        self.coletar(13 * 3600, STAT2, DISK2, NET2)
        d = Path(self.dados) / "monitor"
        self.assertTrue((d / "2026-10-10.jsonl").exists() and (d / "2026-10-11.jsonl").exists())


class SerieTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados = tmp.name
        self.d = Path(tmp.name) / "monitor"
        self.d.mkdir()
        self.agora = dt.datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)

    def escrever(self, minutos_atras, **campos):
        t = self.agora - dt.timedelta(minutes=minutos_atras)
        p = {"t": t.isoformat(timespec="seconds"), "cpu": None, "nucleos": [1.0, 2.0], "carga": [1.0, 1.0, 1.0], "n_cpu": 2}
        p.update(campos)
        with open(self.d / f"{t:%Y-%m-%d}.jsonl", "a") as f:
            f.write(json.dumps(p) + "\n")

    def test_janela_filtra_e_ordena_atravessando_dias(self):
        for m in (5, 30, 70, 130, 1000):
            self.escrever(m, cpu=float(m))
        (self.d / "2026-10-10.jsonl").open("a").write("linha quebrada\n{\"sem_t\": 1}\n")
        self.assertEqual([p["cpu"] for p in monitor.serie("1h", self.dados, self.agora)], [30.0, 5.0])
        self.assertEqual([p["cpu"] for p in monitor.serie("6h", self.dados, self.agora)], [130.0, 70.0, 30.0, 5.0])
        self.assertEqual(len(monitor.serie("24h", self.dados, self.agora)), 5)  # 1000 min atrás cai no dia anterior (19:20 de 09/10)
        self.assertEqual(monitor.serie("7d", self.dados, self.agora)[0]["cpu"], 1000.0)

    def test_sem_arquivos(self):
        self.assertEqual(monitor.serie("1h", str(Path(self.dados) / "vazio"), self.agora), [])

    def test_reducao_a_no_maximo_360_pontos_por_media(self):
        for m in range(0, 1440):
            self.escrever(m, cpu=float(m % 2) * 10 if m % 4 else None, carga=[float(m), 0.0, 0.0])
        s = monitor.serie("24h", self.dados, self.agora)
        self.assertLessEqual(len(s), monitor.MAX_PONTOS)
        self.assertEqual(len(s), 360)  # 1439 pontos dentro da janela / baldes de 4
        self.assertEqual(s[0]["t"] < s[-1]["t"], True)
        # balde de 4 minutos vizinhos: carga é a média dos 4 valores, "t" é o do fim do balde
        self.assertEqual(s[-1]["carga"][0], 1.5)
        self.assertEqual(s[-1]["t"], self.agora.isoformat(timespec="seconds"))

    def test_nulls_ignorados_na_media_e_balde_so_de_nulls_vira_null(self):
        pts = [{"t": f"2026-10-10T00:{i:02d}:00+00:00", "cpu": c, "nucleos": [c], "n_cpu": 4}
               for i, c in enumerate([10.0, None, 30.0, None, None, None])]
        r = monitor.reduzir(pts, 2)
        self.assertEqual([p["cpu"] for p in r], [20.0, None])
        self.assertEqual(r[0]["nucleos"], [20.0])
        self.assertEqual(r[1]["nucleos"], [None])
        self.assertEqual(r[0]["t"], pts[2]["t"])

    def test_poucos_pontos_nao_sao_alterados(self):
        self.escrever(3, cpu=1.0)
        self.assertEqual(len(monitor.serie("1h", self.dados, self.agora)), 1)


class MainTest(unittest.TestCase):
    def test_coletar_nao_imprime_nada_e_ver_mostra(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HUB_DATA": tmp}), \
                mock.patch.multiple(monitor, docker_stats=lambda: None, docker_df=lambda agora=None: None):
            buf = io.StringIO()
            with redirect_stdout(buf), mock.patch("sys.stdout.reconfigure", create=True):
                self.assertEqual(monitor.main(["coletar"]), 0)
            self.assertEqual(buf.getvalue(), "")
            buf = io.StringIO()
            with redirect_stdout(buf), mock.patch("sys.stdout.reconfigure", create=True):
                monitor.main(["ver"])
            self.assertIn('"ponto"', buf.getvalue())


if __name__ == "__main__":
    unittest.main()


class RemotosTest(unittest.TestCase):
    def test_cpu_da_vps_remota_pela_diferenca(self):
        mem, load = "MemTotal: 1000 kB\nMemAvailable: 250 kB\n", "1.0 2.0 3.0 1/2 3\n"
        p1, st1 = monitor.ponto_remoto("cpu 100 0 100 800 0 0 0 0\ncpu0 1 0 1 8\n@@@\n" + mem + "@@@\n" + load)
        p2, _ = monitor.ponto_remoto("cpu 150 0 150 900 0 0 0 0\ncpu0 1 0 1 8\n@@@\n" + mem + "@@@\n" + load, st1)
        self.assertIsNone(p1["cpu"])
        self.assertEqual((p2["cpu"], p2["mem_pct"], p2["carga"][0], p2["n_cpu"]), (50.0, 75.0, 1.0, 1))

    def test_sem_resposta_guarda_a_ultima_e_marca_erro(self):
        with tempfile.TemporaryDirectory() as d:
            r = monitor.coletar_remotos(d, rodar=lambda *a, **k: (255, "", "recusado"))
        self.assertEqual(r["servidor_a"]["erro"], "sem resposta")


    def test_deploys_do_clientej_vem_do_log(self):
        saidas = iter([(0, "cpu 1 0 1 8 0 0 0 0\n@@@\nMemTotal: 10 kB\nMemAvailable: 5 kB\n@@@\n0.1 0.1 0.1 1/1 1\n", ""),
                       (0, "2026-10-16T00:14:46+00:00 no ar: 274a83c\n", ""), (255, "", ""), (255, "", "")])
        with tempfile.TemporaryDirectory() as d:
            r = monitor.coletar_remotos(d, rodar=lambda *a, **k: next(saidas))
        self.assertEqual(r["servidor_a"]["deploys"], [{"em": "2026-10-16T00:14:46+00:00", "sha": "274a83c"}])


    def test_pc_windows(self):
        p = monitor.ponto_pc("cpu:41\nmem:63,5\ngpu:7, 1394, 8188\n")
        self.assertEqual((p["cpu"], p["mem_pct"], p["gpu"], p["gpu_mem_pct"]), (41.0, 63.5, 7.0, 17.0))
        self.assertIsNone(monitor.ponto_pc("")["cpu"])

class RemotosCacheTest(unittest.TestCase):
    def test_disco_remoto_e_cache_apos_falha(self):
        texto = 'cpu 150 0 150 900 0 0 0 0\n@@@\nMemTotal: 1000 kB\nMemAvailable: 250 kB\n@@@\n1 2 3 1/2 3\n@@@\nFilesystem 1024-blocks Used Available Capacity Mounted on\n/dev/vda1 1000 730 270 73% /\n'
        p, _ = monitor.ponto_remoto(texto, (800,1000))
        self.assertEqual((p['cpu'],p['mem_pct'],p['disco_pct']),(50,75,73))
        with tempfile.TemporaryDirectory() as d:
            arq=monitor.pasta(d)/'remotos.json'
            arq.parent.mkdir(parents=True,exist_ok=True)
            antigo={'cpu':41,'mem_pct':62,'disco_pct':73,'t':'2026-10-16T01:00:00Z','stat':[800,1000]}
            arq.write_text(json.dumps({'servidor_a':antigo,'servidor_b':antigo}))
            r=monitor.coletar_remotos(d,rodar=lambda *a,**k:(255,'','falhou'))
            for s in ['servidor_a','servidor_b']:
                self.assertEqual({k:r[s][k] for k in antigo},antigo)

class TestRodarBytesInvalidos(unittest.TestCase):
    def test_saida_nao_utf8_nao_quebra(self):
        from hub import monitor
        cod, out, _ = monitor._rodar(["printf", "ok\\242"])
        self.assertEqual(cod, 0)
        self.assertTrue(out.startswith("ok"))
