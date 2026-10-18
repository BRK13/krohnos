// Imprime em JSON os metadados das regras do SEOmator ({regra: {name, description, fix}}), lidos do último audit do banco.
// Roda de /opt/seomator (copiado por deploy/seomator.sh) para achar o pacote; respeita SEOMATOR_HOME.
import * as m from '@seomator/seo-audit';

const db = m.getAuditsDatabase();
const ultimo = m.listAudits(db, { limit: 1 })[0];
// Sem audit no banco não há metadados: {} e sai 0.
console.log(JSON.stringify(ultimo ? m.getAuditDetail(db, ultimo.auditId).ruleMetadata : {}));
