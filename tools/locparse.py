"""Turn free-text job locations (+ JD text) into structured fields for faceting.
parse(location, jd) -> {"remote": "remote|hybrid|onsite|unknown", "countries": [...], "regions": [...], "cities": [...]}
Heuristic, deterministic, no network. Countries are ISO-3166 alpha-2; US regions are state codes."""
import re

US_STATES = {"AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California","CO":"Colorado","CT":"Connecticut","DE":"Delaware","FL":"Florida","GA":"Georgia","HI":"Hawaii","ID":"Idaho","IL":"Illinois","IN":"Indiana","IA":"Iowa","KS":"Kansas","KY":"Kentucky","LA":"Louisiana","ME":"Maine","MD":"Maryland","MA":"Massachusetts","MI":"Michigan","MN":"Minnesota","MS":"Mississippi","MO":"Missouri","MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire","NJ":"New Jersey","NM":"New Mexico","NY":"New York","NC":"North Carolina","ND":"North Dakota","OH":"Ohio","OK":"Oklahoma","OR":"Oregon","PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina","SD":"South Dakota","TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont","VA":"Virginia","WA":"Washington","WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming","DC":"District of Columbia"}
STATE_BY_NAME = {v.lower(): k for k, v in US_STATES.items()}
CA_PROV = {"ON":"Ontario","QC":"Quebec","BC":"British Columbia","AB":"Alberta","MB":"Manitoba","SK":"Saskatchewan","NS":"Nova Scotia","NB":"New Brunswick"}
PROV_BY_NAME = {v.lower(): k for k, v in CA_PROV.items()}
COUNTRIES = {
 "united states":"US","usa":"US","u.s.":"US","u.s.a.":"US","us":"US","america":"US","united states of america":"US",
 "united kingdom":"GB","uk":"GB","england":"GB","scotland":"GB","wales":"GB","great britain":"GB","northern ireland":"GB",
 "canada":"CA","germany":"DE","deutschland":"DE","france":"FR","spain":"ES","españa":"ES","italy":"IT","italia":"IT","netherlands":"NL","the netherlands":"NL","belgium":"BE","switzerland":"CH","austria":"AT","sweden":"SE","norway":"NO","denmark":"DK","finland":"FI","ireland":"IE","poland":"PL","polska":"PL","portugal":"PT","czech republic":"CZ","czechia":"CZ","hungary":"HU","romania":"RO","greece":"GR","ukraine":"UA","türkiye":"TR","turkey":"TR",
 "india":"IN","china":"CN","japan":"JP","south korea":"KR","korea":"KR","singapore":"SG","hong kong":"HK","taiwan":"TW","australia":"AU","new zealand":"NZ","philippines":"PH","indonesia":"ID","malaysia":"MY","thailand":"TH","vietnam":"VN","pakistan":"PK","bangladesh":"BD","sri lanka":"LK",
 "mexico":"MX","méxico":"MX","brazil":"BR","brasil":"BR","argentina":"AR","colombia":"CO","chile":"CL","peru":"PE","costa rica":"CR","uruguay":"UY",
 "israel":"IL","united arab emirates":"AE","uae":"AE","dubai":"AE","saudi arabia":"SA","qatar":"QA","egypt":"EG","south africa":"ZA","nigeria":"NG","kenya":"KE","morocco":"MA",
}
CITY_COUNTRY = {  # frequent cities without an explicit country
 "london":"GB","manchester":"GB","birmingham":"GB","edinburgh":"GB","dublin":"IE","paris":"FR","berlin":"DE","munich":"DE","münchen":"DE","hamburg":"DE","frankfurt":"DE","amsterdam":"NL","rotterdam":"NL","brussels":"BE","zurich":"CH","zürich":"CH","geneva":"CH","vienna":"AT","wien":"AT","stockholm":"SE","oslo":"NO","copenhagen":"DK","helsinki":"FI","warsaw":"PL","warszawa":"PL","krakow":"PL","kraków":"PL","prague":"CZ","praha":"CZ","lisbon":"PT","lisboa":"PT","madrid":"ES","barcelona":"ES","milan":"IT","milano":"IT","rome":"IT","athens":"GR","tel aviv":"IL","bangalore":"IN","bengaluru":"IN","hyderabad":"IN","pune":"IN","mumbai":"IN","chennai":"IN","gurgaon":"IN","gurugram":"IN","noida":"IN","delhi":"IN","new delhi":"IN","singapore":"SG","tokyo":"JP","seoul":"KR","sydney":"AU","melbourne":"AU","toronto":"CA","vancouver":"CA","montreal":"CA","montréal":"CA","ottawa":"CA","calgary":"CA","mexico city":"MX","são paulo":"BR","sao paulo":"BR","buenos aires":"AR","bogotá":"CO","bogota":"CO","santiago":"CL","lima":"PE","dubai":"AE","hong kong":"HK","taipei":"TW","manila":"PH","jakarta":"ID","kuala lumpur":"MY","bangkok":"TH","ho chi minh city":"VN","hanoi":"VN","cape town":"ZA","johannesburg":"ZA","lagos":"NG","nairobi":"KE",
 "new york":"US","new york city":"US","nyc":"US","san francisco":"US","sf":"US","los angeles":"US","seattle":"US","austin":"US","boston":"US","chicago":"US","denver":"US","atlanta":"US","miami":"US","dallas":"US","houston":"US","washington":"US","san jose":"US","san diego":"US","portland":"US","phoenix":"US","philadelphia":"US","minneapolis":"US","salt lake city":"US","raleigh":"US","charlotte":"US","nashville":"US","pittsburgh":"US","detroit":"US","columbus":"US","san francisco bay area":"US","bay area":"US","silicon valley":"US","remote - usa":"US",
}
REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|distributed|anywhere)\b", re.I)
HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
# words that mean remote *work* ("distributed systems" and "work from anywhere in the office" do not)
REMOTE_WORDS = re.compile(r"\bremote(?:ly|-friendly|-first)?\b|\bwork(?:ing)? from home\b|\bwfh\b|\btelecommut", re.I)
ONSITE_RE = re.compile(r"\b(on-?site|in-?office|in person)\b", re.I)

ISO3 = {"usa":"US","gbr":"GB","ind":"IN","can":"CA","deu":"DE","fra":"FR","aus":"AU","nld":"NL","esp":"ES","ita":"IT","pol":"PL","prt":"PT","irl":"IE","swe":"SE","che":"CH","sgp":"SG","jpn":"JP","bra":"BR","mex":"MX","isr":"IL","are":"AE","phl":"PH","idn":"ID","mys":"MY","vnm":"VN","kor":"KR","chn":"CN","hkg":"HK","twn":"TW","nzl":"NZ","arg":"AR","col":"CO","chl":"CL","zaf":"ZA","nga":"NG","ken":"KE","ukr":"UA","tur":"TR","rou":"RO","hun":"HU","cze":"CZ","grc":"GR","aut":"AT","bel":"BE","dnk":"DK","nor":"NO","fin":"FI"}
COUNTRIES.update(ISO3)
# ISO-3166 names (official + common, generated from pycountry) and alpha-3 codes. Aliases above win on conflicts.
ISO_NAMES = {"afghanistan": "AF", "albania": "AL", "algeria": "DZ", "american samoa": "AS", "andorra": "AD", "angola": "AO", "anguilla": "AI", "antarctica": "AQ", "antigua and barbuda": "AG", "arab republic of egypt": "EG", "argentina": "AR", "argentine republic": "AR", "armenia": "AM", "aruba": "AW", "ascension and tristan da cunha saint helena": "SH", "australia": "AU", "austria": "AT", "azerbaijan": "AZ", "bahamas": "BS", "bahrain": "BH", "bangladesh": "BD", "barbados": "BB", "belarus": "BY", "belgium": "BE", "belize": "BZ", "benin": "BJ", "bermuda": "BM", "bhutan": "BT", "bolivarian republic of venezuela": "VE", "bolivia": "BO", "bolivia, plurinational state of": "BO", "bonaire": "BQ", "bonaire, sint eustatius and saba": "BQ", "bosnia and herzegovina": "BA", "botswana": "BW", "bouvet island": "BV", "brazil": "BR", "british indian ocean territory": "IO", "british virgin islands": "VG", "brunei darussalam": "BN", "bulgaria": "BG", "burkina faso": "BF", "burundi": "BI", "cabo verde": "CV", "cambodia": "KH", "cameroon": "CM", "canada": "CA", "cayman islands": "KY", "central african republic": "CF", "chad": "TD", "chile": "CL", "china": "CN", "christmas island": "CX", "cocos (keeling) islands": "CC", "colombia": "CO", "commonwealth of dominica": "DM", "commonwealth of the bahamas": "BS", "commonwealth of the northern mariana islands": "MP", "comoros": "KM", "congo, the democratic republic of the": "CD", "cook islands": "CK", "costa rica": "CR", "croatia": "HR", "cuba": "CU", "curaçao": "CW", "cyprus": "CY", "czech republic": "CZ", "czechia": "CZ", "côte d'ivoire": "CI", "democratic people's republic of korea": "KP", "democratic republic of sao tome and principe": "ST", "democratic republic of timor-leste": "TL", "democratic socialist republic of sri lanka": "LK", "denmark": "DK", "djibouti": "DJ", "dominica": "DM", "dominican republic": "DO", "eastern republic of uruguay": "UY", "ecuador": "EC", "egypt": "EG", "el salvador": "SV", "equatorial guinea": "GQ", "eritrea": "ER", "estonia": "EE", "eswatini": "SZ", "ethiopia": "ET", "falkland islands (malvinas)": "FK", "faroe islands": "FO", "federal democratic republic of ethiopia": "ET", "federal democratic republic of nepal": "NP", "federal republic of germany": "DE", "federal republic of nigeria": "NG", "federal republic of somalia": "SO", "federated states of micronesia": "FM", "federative republic of brazil": "BR", "fiji": "FJ", "finland": "FI", "france": "FR", "french guiana": "GF", "french polynesia": "PF", "french republic": "FR", "french southern territories": "TF", "gabon": "GA", "gabonese republic": "GA", "gambia": "GM", "germany": "DE", "ghana": "GH", "gibraltar": "GI", "grand duchy of luxembourg": "LU", "greece": "GR", "greenland": "GL", "grenada": "GD", "guadeloupe": "GP", "guam": "GU", "guatemala": "GT", "guernsey": "GG", "guinea": "GN", "guinea-bissau": "GW", "guyana": "GY", "haiti": "HT", "hashemite kingdom of jordan": "JO", "heard island and mcdonald islands": "HM", "hellenic republic": "GR", "holy see (vatican city state)": "VA", "honduras": "HN", "hong kong": "HK", "hong kong special administrative region of china": "HK", "hungary": "HU", "iceland": "IS", "independent state of papua new guinea": "PG", "independent state of samoa": "WS", "india": "IN", "indonesia": "ID", "iran": "IR", "iran, islamic republic of": "IR", "iraq": "IQ", "ireland": "IE", "islamic republic of afghanistan": "AF", "islamic republic of iran": "IR", "islamic republic of mauritania": "MR", "islamic republic of pakistan": "PK", "isle of man": "IM", "israel": "IL", "italian republic": "IT", "italy": "IT", "jamaica": "JM", "japan": "JP", "jersey": "JE", "jordan": "JO", "kazakhstan": "KZ", "kenya": "KE", "kingdom of bahrain": "BH", "kingdom of belgium": "BE", "kingdom of bhutan": "BT", "kingdom of cambodia": "KH", "kingdom of denmark": "DK", "kingdom of eswatini": "SZ", "kingdom of lesotho": "LS", "kingdom of morocco": "MA", "kingdom of norway": "NO", "kingdom of saudi arabia": "SA", "kingdom of spain": "ES", "kingdom of sweden": "SE", "kingdom of thailand": "TH", "kingdom of the netherlands": "NL", "kingdom of tonga": "TO", "kiribati": "KI", "korea, democratic people's republic of": "KP", "korea, republic of": "KR", "kuwait": "KW", "kyrgyz republic": "KG", "kyrgyzstan": "KG", "lao people's democratic republic": "LA", "laos": "LA", "latvia": "LV", "lebanese republic": "LB", "lebanon": "LB", "lesotho": "LS", "liberia": "LR", "libya": "LY", "liechtenstein": "LI", "lithuania": "LT", "luxembourg": "LU", "macao": "MO", "macao special administrative region of china": "MO", "madagascar": "MG", "malawi": "MW", "malaysia": "MY", "maldives": "MV", "mali": "ML", "malta": "MT", "marshall islands": "MH", "martinique": "MQ", "mauritania": "MR", "mauritius": "MU", "mayotte": "YT", "mexico": "MX", "micronesia, federated states of": "FM", "moldova": "MD", "moldova, republic of": "MD", "monaco": "MC", "mongolia": "MN", "montenegro": "ME", "montserrat": "MS", "morocco": "MA", "mozambique": "MZ", "myanmar": "MM", "namibia": "NA", "nauru": "NR", "nepal": "NP", "netherlands": "NL", "new caledonia": "NC", "new zealand": "NZ", "nicaragua": "NI", "niger": "NE", "nigeria": "NG", "niue": "NU", "norfolk island": "NF", "north korea": "KP", "north macedonia": "MK", "northern mariana islands": "MP", "norway": "NO", "oman": "OM", "pakistan": "PK", "palau": "PW", "palestine": "PS", "palestine, state of": "PS", "panama": "PA", "papua new guinea": "PG", "paraguay": "PY", "people's democratic republic of algeria": "DZ", "people's republic of bangladesh": "BD", "people's republic of china": "CN", "peru": "PE", "philippines": "PH", "pitcairn": "PN", "plurinational state of bolivia": "BO", "poland": "PL", "portugal": "PT", "portuguese republic": "PT", "principality of andorra": "AD", "principality of liechtenstein": "LI", "principality of monaco": "MC", "province of china taiwan": "TW", "puerto rico": "PR", "qatar": "QA", "republic of albania": "AL", "republic of angola": "AO", "republic of armenia": "AM", "republic of austria": "AT", "republic of azerbaijan": "AZ", "republic of belarus": "BY", "republic of benin": "BJ", "republic of bosnia and herzegovina": "BA", "republic of botswana": "BW", "republic of bulgaria": "BG", "republic of burundi": "BI", "republic of cabo verde": "CV", "republic of cameroon": "CM", "republic of chad": "TD", "republic of chile": "CL", "republic of colombia": "CO", "republic of costa rica": "CR", "republic of croatia": "HR", "republic of cuba": "CU", "republic of cyprus": "CY", "republic of côte d'ivoire": "CI", "republic of djibouti": "DJ", "republic of ecuador": "EC", "republic of el salvador": "SV", "republic of equatorial guinea": "GQ", "republic of estonia": "EE", "republic of fiji": "FJ", "republic of finland": "FI", "republic of ghana": "GH", "republic of guatemala": "GT", "republic of guinea": "GN", "republic of guinea-bissau": "GW", "republic of guyana": "GY", "republic of haiti": "HT", "republic of honduras": "HN", "republic of iceland": "IS", "republic of india": "IN", "republic of indonesia": "ID", "republic of iraq": "IQ", "republic of kazakhstan": "KZ", "republic of kenya": "KE", "republic of kiribati": "KI", "republic of korea": "KR", "republic of latvia": "LV", "republic of liberia": "LR", "republic of lithuania": "LT", "republic of madagascar": "MG", "republic of malawi": "MW", "republic of maldives": "MV", "republic of mali": "ML", "republic of malta": "MT", "republic of mauritius": "MU", "republic of moldova": "MD", "republic of mozambique": "MZ", "republic of myanmar": "MM", "republic of namibia": "NA", "republic of nauru": "NR", "republic of nicaragua": "NI", "republic of north macedonia": "MK", "republic of palau": "PW", "republic of panama": "PA", "republic of paraguay": "PY", "republic of peru": "PE", "republic of poland": "PL", "republic of san marino": "SM", "republic of senegal": "SN", "republic of serbia": "RS", "republic of seychelles": "SC", "republic of sierra leone": "SL", "republic of singapore": "SG", "republic of slovenia": "SI", "republic of south africa": "ZA", "republic of south sudan": "SS", "republic of suriname": "SR", "republic of tajikistan": "TJ", "republic of the congo": "CG", "republic of the gambia": "GM", "republic of the marshall islands": "MH", "republic of the niger": "NE", "republic of the philippines": "PH", "republic of the sudan": "SD", "republic of trinidad and tobago": "TT", "republic of tunisia": "TN", "republic of türkiye": "TR", "republic of uganda": "UG", "republic of uzbekistan": "UZ", "republic of vanuatu": "VU", "republic of yemen": "YE", "republic of zambia": "ZM", "republic of zimbabwe": "ZW", "romania": "RO", "russian federation": "RU", "rwanda": "RW", "rwandese republic": "RW", "réunion": "RE", "saint barthélemy": "BL", "saint helena": "SH", "saint helena, ascension and tristan da cunha": "SH", "saint kitts and nevis": "KN", "saint lucia": "LC", "saint martin (french part)": "MF", "saint pierre and miquelon": "PM", "saint vincent and the grenadines": "VC", "samoa": "WS", "san marino": "SM", "sao tome and principe": "ST", "saudi arabia": "SA", "senegal": "SN", "serbia": "RS", "seychelles": "SC", "sierra leone": "SL", "singapore": "SG", "sint eustatius and saba bonaire": "BQ", "sint maarten (dutch part)": "SX", "slovak republic": "SK", "slovakia": "SK", "slovenia": "SI", "socialist republic of viet nam": "VN", "solomon islands": "SB", "somalia": "SO", "south africa": "ZA", "south georgia and the south sandwich islands": "GS", "south korea": "KR", "south sudan": "SS", "spain": "ES", "sri lanka": "LK", "state of israel": "IL", "state of kuwait": "KW", "state of palestine": "PS", "state of qatar": "QA", "sudan": "SD", "sultanate of oman": "OM", "suriname": "SR", "svalbard and jan mayen": "SJ", "sweden": "SE", "swiss confederation": "CH", "switzerland": "CH", "syria": "SY", "syrian arab republic": "SY", "taiwan": "TW", "taiwan, province of china": "TW", "tajikistan": "TJ", "tanzania": "TZ", "tanzania, united republic of": "TZ", "thailand": "TH", "the democratic republic of the congo": "CD", "the state of eritrea": "ER", "the state of palestine": "PS", "timor-leste": "TL", "togo": "TG", "togolese republic": "TG", "tokelau": "TK", "tonga": "TO", "trinidad and tobago": "TT", "tunisia": "TN", "turkmenistan": "TM", "turks and caicos islands": "TC", "tuvalu": "TV", "türkiye": "TR", "u.s. virgin islands": "VI", "uganda": "UG", "ukraine": "UA", "union of the comoros": "KM", "united arab emirates": "AE", "united kingdom": "GB", "united kingdom of great britain and northern ireland": "GB", "united mexican states": "MX", "united republic of tanzania": "TZ", "united states": "US", "united states minor outlying islands": "UM", "united states of america": "US", "uruguay": "UY", "uzbekistan": "UZ", "vanuatu": "VU", "venezuela": "VE", "venezuela, bolivarian republic of": "VE", "viet nam": "VN", "vietnam": "VN", "virgin islands of the united states": "VI", "virgin islands, british": "VG", "virgin islands, u.s.": "VI", "wallis and futuna": "WF", "western sahara": "EH", "yemen": "YE", "zambia": "ZM", "zimbabwe": "ZW", "åland islands": "AX"}
ISO3_ALL = {"abw": "AW", "afg": "AF", "ago": "AO", "aia": "AI", "ala": "AX", "alb": "AL", "and": "AD", "are": "AE", "arg": "AR", "arm": "AM", "asm": "AS", "ata": "AQ", "atf": "TF", "atg": "AG", "aus": "AU", "aut": "AT", "aze": "AZ", "bdi": "BI", "bel": "BE", "ben": "BJ", "bes": "BQ", "bfa": "BF", "bgd": "BD", "bgr": "BG", "bhr": "BH", "bhs": "BS", "bih": "BA", "blm": "BL", "blr": "BY", "blz": "BZ", "bmu": "BM", "bol": "BO", "bra": "BR", "brb": "BB", "brn": "BN", "btn": "BT", "bvt": "BV", "bwa": "BW", "caf": "CF", "can": "CA", "cck": "CC", "che": "CH", "chl": "CL", "chn": "CN", "civ": "CI", "cmr": "CM", "cod": "CD", "cog": "CG", "cok": "CK", "col": "CO", "com": "KM", "cpv": "CV", "cri": "CR", "cub": "CU", "cuw": "CW", "cxr": "CX", "cym": "KY", "cyp": "CY", "cze": "CZ", "deu": "DE", "dji": "DJ", "dma": "DM", "dnk": "DK", "dom": "DO", "dza": "DZ", "ecu": "EC", "egy": "EG", "eri": "ER", "esh": "EH", "esp": "ES", "est": "EE", "eth": "ET", "fin": "FI", "fji": "FJ", "flk": "FK", "fra": "FR", "fro": "FO", "fsm": "FM", "gab": "GA", "gbr": "GB", "geo": "GE", "ggy": "GG", "gha": "GH", "gib": "GI", "gin": "GN", "glp": "GP", "gmb": "GM", "gnb": "GW", "gnq": "GQ", "grc": "GR", "grd": "GD", "grl": "GL", "gtm": "GT", "guf": "GF", "gum": "GU", "guy": "GY", "hkg": "HK", "hmd": "HM", "hnd": "HN", "hrv": "HR", "hti": "HT", "hun": "HU", "idn": "ID", "imn": "IM", "ind": "IN", "iot": "IO", "irl": "IE", "irn": "IR", "irq": "IQ", "isl": "IS", "isr": "IL", "ita": "IT", "jam": "JM", "jey": "JE", "jor": "JO", "jpn": "JP", "kaz": "KZ", "ken": "KE", "kgz": "KG", "khm": "KH", "kir": "KI", "kna": "KN", "kor": "KR", "kwt": "KW", "lao": "LA", "lbn": "LB", "lbr": "LR", "lby": "LY", "lca": "LC", "lie": "LI", "lka": "LK", "lso": "LS", "ltu": "LT", "lux": "LU", "lva": "LV", "mac": "MO", "maf": "MF", "mar": "MA", "mco": "MC", "mda": "MD", "mdg": "MG", "mdv": "MV", "mex": "MX", "mhl": "MH", "mkd": "MK", "mli": "ML", "mlt": "MT", "mmr": "MM", "mne": "ME", "mng": "MN", "mnp": "MP", "moz": "MZ", "mrt": "MR", "msr": "MS", "mtq": "MQ", "mus": "MU", "mwi": "MW", "mys": "MY", "myt": "YT", "nam": "NA", "ncl": "NC", "ner": "NE", "nfk": "NF", "nga": "NG", "nic": "NI", "niu": "NU", "nld": "NL", "nor": "NO", "npl": "NP", "nru": "NR", "nzl": "NZ", "omn": "OM", "pak": "PK", "pan": "PA", "pcn": "PN", "per": "PE", "phl": "PH", "plw": "PW", "png": "PG", "pol": "PL", "pri": "PR", "prk": "KP", "prt": "PT", "pry": "PY", "pse": "PS", "pyf": "PF", "qat": "QA", "reu": "RE", "rou": "RO", "rus": "RU", "rwa": "RW", "sau": "SA", "sdn": "SD", "sen": "SN", "sgp": "SG", "sgs": "GS", "shn": "SH", "sjm": "SJ", "slb": "SB", "sle": "SL", "slv": "SV", "smr": "SM", "som": "SO", "spm": "PM", "srb": "RS", "ssd": "SS", "stp": "ST", "sur": "SR", "svk": "SK", "svn": "SI", "swe": "SE", "swz": "SZ", "sxm": "SX", "syc": "SC", "syr": "SY", "tca": "TC", "tcd": "TD", "tgo": "TG", "tha": "TH", "tjk": "TJ", "tkl": "TK", "tkm": "TM", "tls": "TL", "ton": "TO", "tto": "TT", "tun": "TN", "tur": "TR", "tuv": "TV", "twn": "TW", "tza": "TZ", "uga": "UG", "ukr": "UA", "umi": "UM", "ury": "UY", "usa": "US", "uzb": "UZ", "vat": "VA", "vct": "VC", "ven": "VE", "vgb": "VG", "vir": "VI", "vnm": "VN", "vut": "VU", "wlf": "WF", "wsm": "WS", "yem": "YE", "zaf": "ZA", "zmb": "ZM", "zwe": "ZW"}
for _k, _v in ISO_NAMES.items(): COUNTRIES.setdefault(_k, _v)
for _k, _v in ISO3_ALL.items(): COUNTRIES.setdefault(_k, _v)
# "Georgia" is both a US state and a country: bare "Georgia" stays the state (far more common in job
# postings); these forms, or a Georgian city in the same segment, mean the country.
COUNTRIES.update({"nederland": "NL", "österreich": "AT", "oesterreich": "AT", "schweiz": "CH", "suisse": "CH", "svizzera": "CH", "polska": "PL", "česko": "CZ", "cesko": "CZ",
                  "česká republika": "CZ", "sverige": "SE", "norge": "NO", "danmark": "DK", "suomi": "FI", "magyarország": "HU", "românia": "RO", "belgië": "BE", "belgique": "BE",
                  "éire": "IE", "lietuva": "LT", "latvija": "LV", "eesti": "EE", "hrvatska": "HR", "srbija": "RS", "slovensko": "SK", "slovenija": "SI", "българия": "BG", "ísland": "IS",
                  "perú": "PE", "日本": "JP", "中国": "CN", "대한민국": "KR", "한국": "KR", "việt nam": "VN", "भारत": "IN", "россия": "RU", "україна": "UA", "ελλάδα": "GR", "台灣": "TW", "香港": "HK",
                  "المملكة العربية السعودية": "SA", "الإمارات": "AE", "मुंबई": "IN", "united states of america (usa)": "US"})
COUNTRIES.update({"democratic republic of the congo": "CD", "dr congo": "CD", "drc": "CD", "congo": "CG", "republic of the congo": "CG", "congo-brazzaville": "CG", "congo-kinshasa": "CD",
                  "georgia (country)": "GE", "country of georgia": "GE", "republic of georgia": "GE", "sakartvelo": "GE", "georgia country": "GE"})
CITY_COUNTRY.update({"tbilisi": "GE", "batumi": "GE", "kutaisi": "GE"})
import os as _os, json as _json
try:  # ~5k bare city names that resolve to one country ≥90% of the time in the corpus (build-city-table.py)
    for _k, _v in _json.load(open(_os.path.join(_os.path.dirname(__file__), "city_countries.json"), encoding="utf-8")).items(): CITY_COUNTRY.setdefault(_k, _v)
except Exception: pass
# optional fallback for strings the rules can't place: {location string: [country, confidence]} from the
# published location-countries table (nearest-neighbour vote over location-string embeddings). jobs.py fills it.
LOC_TABLE = {}
GEORGIAN_CITIES = {"tbilisi", "batumi", "kutaisi", "rustavi"}
TZ_CODES = {"EST", "PST", "CST", "MST", "HST", "AKST", "EDT", "PDT", "CDT", "MDT", "GMT", "UTC", "CET", "IST", "BST", "AEST"}

def _split(loc):
    # hyphenated forms: "Mexico-Remote", "US - Remote (…)", "IND-Pune-Smartworks" -> split on hyphens too when a
    # side is a known country/code (keeps "Winston-Salem" intact)
    def hy(m):
        a, b = m.group(1), m.group(2)
        if f"{a}-{b}".lower() in COUNTRIES: return m.group(0)  # Guinea-Bissau, Timor-Leste
        if b.isupper() and len(b) == 2 and b in US_STATES and a[:1].isupper(): return f"{a}, {b}"  # "Boston-MA"
        return f"{a}; {b}" if (a.lower() in COUNTRIES or b.lower() in COUNTRIES or REMOTE_RE.fullmatch(a) or REMOTE_RE.fullmatch(b)) else m.group(0)
    loc = re.sub(r"\bgeorgia\s*\(\s*country\s*\)", "georgia country", loc, flags=re.I)  # "(country)" must not become its own segment
    loc = re.sub(r"\b([A-Za-z.]+(?: [A-Za-z.]+)?)\s*[-–]\s*([A-Za-z.]+)\b", hy, loc)  # also "Czech Republic-Prague"
    loc = loc.replace(">", ";")  # "Hungary > Budapest"
    loc = re.sub(r"[()\[\]]", ";", loc)  # "Remote (United States)" -> two segments
    loc = re.sub(r"\b(remote|hybrid|on-?site|work from home|wfh)\b\s*[-–:]?\s*", lambda m: m.group(1) + ";", loc, flags=re.I)  # "Hybrid - Austin" -> "hybrid; Austin"
    parts = re.split(r"\s*(?:;|\||/|:|\bor\b|\band\b|&|•)\s*", loc)  # ":" too: "US: PST or EST"
    return [p.strip(" -–,") for p in parts if p and p.strip(" -–,")]

def _one(seg, out):
    s = seg.strip().strip("()[]")
    if not s: return
    low = s.lower()
    if REMOTE_RE.search(low) and len(low) < 40: out["remote_hint"] = True
    # icims style: US-CA-San Jose / US-Remote
    m = re.match(r"^([A-Z]{2})-([A-Z]{2})-(.+)$", s)
    if m and m.group(1) == "US" and m.group(2) in US_STATES:
        out["countries"].add("US"); out["regions"].add(m.group(2)); out["cities"].add(m.group(3).strip().title()); return
    m = re.match(r"^([A-Z]{2,3})-(.+)$", s)
    if m and m.group(1).lower() in COUNTRIES:
        out["countries"].add(COUNTRIES[m.group(1).lower()]); rest = m.group(2).split("-")[0].strip()
        if rest.lower() != "remote": out["cities"].add(rest.title()); return
        return
    toks = [t.strip() for t in re.split(r",", s) if t.strip()]
    toks2 = []
    for t in toks:  # "SD USA" -> "SD", "USA"
        m2 = re.match(r"^([A-Z]{2})\s+(.+)$", t.strip())
        if m2 and m2.group(1) in US_STATES and m2.group(2).lower().strip(". ") in COUNTRIES: toks2 += [m2.group(1), m2.group(2)]
        elif m2 and m2.group(1) in US_STATES and re.fullmatch(r"\d{5}(?:-\d{4})?", m2.group(2).strip()): toks2.append(m2.group(1))  # "TN 37090"
        else: toks2.append(t)
    toks = toks2
    if len(toks) == 1 and REMOTE_RE.fullmatch(toks[0].strip().lower()): return  # bare "Remote"
    city = None
    explicit = set(); inferred = set()
    lows = [t.lower().strip(". ") for t in toks]
    for t in toks:
        tl = t.lower().strip(" .:")
        if tl == "washington" and any(x in ("dc", "d.c", "district of columbia") for x in lows): out["cities"].add("Washington"); continue
        if tl == "georgia" and any(x in GEORGIAN_CITIES or "country" in x for x in lows): explicit.add("GE"); continue
        if tl in ISO3_ALL and tl not in ISO3:  # generated alpha-3: only as an upper-case token, never a US time zone (EST = Estonia)
            tok = t.strip(" .:")
            if tok == tok.upper() and tok not in TZ_CODES and not re.search(r"time ?zone|hours|\b(pst|est|cst|mst)\b", low): explicit.add(ISO3_ALL[tl])
            continue
        if tl in COUNTRIES: explicit.add(COUNTRIES[tl]); continue
        if t.upper() in US_STATES and (len(t) == 2): explicit.add("US"); out["regions"].add(t.upper()); continue
        if tl in STATE_BY_NAME: explicit.add("US"); out["regions"].add(STATE_BY_NAME[tl]); continue
        if t.upper() in CA_PROV and len(t) == 2: explicit.add("CA"); out["regions"].add("CA-" + t.upper()); continue
        if tl in PROV_BY_NAME: explicit.add("CA"); out["regions"].add("CA-" + PROV_BY_NAME[tl]); continue
        if tl in CITY_COUNTRY:
            inferred.add(CITY_COUNTRY[tl]); out["cities"].add(t.title()); continue
        if REMOTE_RE.search(tl) or tl in ("worldwide", "global", "emea", "apac", "latam", "europe", "eastern europe", "western europe", "north america", "south america", "asia", "africa"):
            if not REMOTE_RE.search(tl): out["regions"].add(tl.title())
            continue
        if city is None and len(t) < 40 and not re.search(r"\d", t) and not REMOTE_RE.search(tl) and not HYBRID_RE.search(tl): city = t
    if city: out["cities"].add(city.title())
    # an explicit country/state wins over a country inferred from a city name (Vienna, VA is not Austria)
    out["countries"] |= explicit if explicit else inferred

def parse(location, jd="", title=""):
    out = {"countries": set(), "regions": set(), "cities": set(), "remote_hint": False}
    for seg in _split(location or ""): _one(seg, out)
    loc = ((location or "") + " | " + (title or "")).lower(); text = (jd or "").lower(); head = text[:2500]
    # An explicit in-office requirement anywhere in the JD beats a "Remote" location (many "Remote - US"
    # postings then say "anchor days Mon/Tue/Fri" or "Monday-Thursday onsite"). Requirement-shaped phrases
    # only, so "hybrid cloud", "onsite interview", "for in-office employees" don't count.
    ONSITE_REQ = re.compile(r"\b(?:\d|one|two|three|four|five)\+? days?(?: a| per| each)? week (?:in|at|from) (?:the |our )?office|anchor days?|"
                            r"\b(?:monday|mon|tuesday|tue|wednesday|wed|thursday|thu|friday|fri)\b[^.\n]{0,40}\b(?:on-?site|in[- ]office|in the office)|"
                            r"\b(?:on-?site|in[- ]office|in the office)\b[^.\n]{0,30}\b(?:monday|mon|tuesday|tue|wednesday|wed|thursday|thu|friday|fri)\b|"
                            r"\b(?:will )?requires? [^.\n]{0,40}\b(?:on-?site|in[- ]office|in the office)|\bability to work on-?site in\b|"
                            r"\b(?:this|the) (?:role|position) is (?:a )?(?:hybrid|on-?site|in-?office)|\bhybrid (?:role|position|schedule|work(?:ing)? model|model)\b", re.I)
    FULL_ONSITE = re.compile(r"\b(?:fully|100%|entirely) (?:on-?site|in[- ]office)\b|\b(?:5|five) days?(?: a| per)? week (?:in|at) (?:the )?office\b|\bno remote\b|\bnot (?:a )?remote\b|"
                            r"\b(?:work(?:ing)? (?:from|out of|in)|based (?:in|out of)|join (?:us|the team) (?:in|at)) our [^.\n]{0,40}\boffices?\b|\bin[- ]person\b[^.\n]{0,30}\boffice\b", re.I)
    if HYBRID_RE.search(loc): remote = "hybrid"
    elif FULL_ONSITE.search(text) and not HYBRID_RE.search(head): remote = "onsite"
    elif ONSITE_REQ.search(text): remote = "hybrid"
    elif HYBRID_RE.search(head): remote = "hybrid"
    elif out["remote_hint"] or REMOTE_RE.search(loc): remote = "remote"
    elif ONSITE_RE.search(loc) or re.search(r"\b(on-?site|in-?office) (role|position|\d+ days)\b|\bthis (role|position) is (on-?site|in-?office)\b", head): remote = "onsite"
    # a role-specific remote statement in the JD counts anywhere; a company blurb ("remote-first", "fully remote team")
    # counts only when the location isn't a specific city
    elif re.search(r"\b(?:100%|fully|entirely) remote (?:role|position|job|opportunity)\b|\bremote (role|position|job)\b|\bthis (role|position) is (?:fully |100% )?remote\b|\bwork(?:ing)? remotely from anywhere\b|\bwork location:?\s*(?:fully |100% )?remote\b|\b(?:fully|100%) remote\s*(?:[—–\-:,.!]|\n|we hire|must|you)", head): remote = "remote"
    elif not out["cities"] and re.search(r"\b(fully|100%|entirely) remote\b|\bremote[- ]first\b", head): remote = "remote"
    else: remote = "unknown"
    est = None
    if not out["countries"] and LOC_TABLE:
        hit = LOC_TABLE.get((location or "").strip()) or LOC_TABLE.get((location or "").strip().lower())
        if hit: est = {"country": hit[0], "p": hit[1]}
    return {"remote": remote, "countries": sorted(out["countries"]), "regions": sorted(out["regions"]), "cities": sorted(out["cities"])[:6], "country_est": est}

if __name__ == "__main__":
    import sys, json
    for t in ["US-CA-San Jose", "Remote - USA", "Paris; Warsaw; Lisboa; Berlin", "Vienna, VA, USA", "Bengaluru, India", "New York City; Remote / San Francisco", "London, United Kingdom", "Hybrid - Austin, TX", "Toronto, ON", "Berlin, Germany; Frankfurt, Germany", "Remote (United States)"]:
        print(f"{t:45s} -> {json.dumps(parse(t))}")


# ---- eligibility against the user's stated location preference ----
RESTRICT_RE = re.compile(r"\b(must (?:be|reside|live|be located|be based)[^.]{0,40}?\b(in|within)\b|(?:only|exclusively) (?:for|open to)? ?(?:candidates|applicants|residents)?[^.]{0,20}?(?:in|based in|located in|from)\b|(?:authori[sz]ed|eligible) to work in|(?:based|located|residing) in|(?:us|u\.s\.|united states)[- ]?(?:only|based)|(?:latam|emea|apac|europe|india|canada|uk)[- ]?only)\b[^.\n]{0,60}", re.I)
MACRO = {"Latam": {"MX","BR","AR","CO","CL","PE","CR","UY"}, "Emea": {"GB","DE","FR","ES","IT","NL","BE","CH","AT","SE","NO","DK","FI","IE","PL","PT","CZ","HU","RO","GR","UA","TR","IL","AE","SA","QA","EG","ZA","NG","KE","MA"}, "Apac": {"IN","CN","JP","KR","SG","HK","TW","AU","NZ","PH","ID","MY","TH","VN","PK","BD","LK"}, "Europe": {"GB","DE","FR","ES","IT","NL","BE","CH","AT","SE","NO","DK","FI","IE","PL","PT","CZ","HU","RO","GR","UA"}, "Eastern Europe": {"PL","CZ","HU","RO","UA","GR"}, "Western Europe": {"GB","DE","FR","ES","IT","NL","BE","CH","AT","IE","PT"}, "North America": {"US","CA","MX"}, "South America": {"BR","AR","CO","CL","PE","UY"}, "Asia": {"IN","CN","JP","KR","SG","HK","TW","PH","ID","MY","TH","VN","PK","BD","LK"}, "Africa": {"ZA","NG","KE","EG","MA"}}

_PLACE_KEYS = sorted(COUNTRIES.keys(), key=len, reverse=True)
_MACRO_KEYS = sorted(MACRO.keys(), key=len, reverse=True)
def find_places(text):
    """Countries (ISO-2) and macro-regions named anywhere in a phrase, e.g. 'must be located in the United States'."""
    t = " " + re.sub(r"[^a-z0-9. ]+", " ", (text or "").lower()) + " "
    cs, rs = set(), set()
    # longest names first, and each match is blanked out, so "northern ireland" is GB (not also IE),
    # "papua new guinea" is PG (not GN), "new mexico" is a US state (not MX)
    for st in sorted(STATE_BY_NAME, key=len, reverse=True):
        if f" {st} " in t: cs.add("US"); t = t.replace(f" {st} ", "  ")
    for k in sorted(_PLACE_KEYS, key=len, reverse=True):
        if len(k) < 3 and k not in ("uk", "us"): continue  # skip 2-letter noise except uk/us
        if k in ISO3_ALL and k not in ("usa",): continue  # alpha-3 codes ("and", "are", "can"…) are words in prose
        if k == "georgia": continue  # bare "georgia" in prose is the state far more often; handled by the state pass above
        if f" {k} " in t or f" {k}. " in t: cs.add(COUNTRIES[k]); t = t.replace(f" {k} ", "  ").replace(f" {k}. ", "  ")
    for k in _MACRO_KEYS:
        if f" {k.lower()} " in t: rs.add(k)
    return cs, rs

def eligibility(pref, location, jd="", title="", arrangement=None):
    """Is a job eligible for someone with location preference `pref` (e.g. "Remote, US", "Berlin",
    "US or Canada")? Returns (eligible: bool|None, reason). None = can't tell (no location info).
    Rules: the job's countries must intersect the user's; a remote job with no country and no
    restricting phrase is eligible; region-restricted remotes (Remote - LATAM, India-only) are not;
    JD phrases like "must be located in the US" / "authorized to work in the UK" restrict too.
    A remote-only preference (no city, e.g. "Remote, US") makes onsite and hybrid jobs ineligible;
    `arrangement` overrides the parsed one (pass the enrichment's work_arrangement when known)."""
    p = parse(pref or "")
    j = parse(location or "", jd, title)
    rm = arrangement if arrangement in ("remote", "hybrid", "onsite") else j["remote"]
    # remote-only preference: the job must actually be labelled remote (location, title, or an explicit
    # "this role is remote" statement). Unknown is not remote.
    if p["remote"] == "remote" and not p["cities"]:
        if rm in ("onsite", "hybrid"): return False, f"{rm} (you asked for remote)"
        if rm != "remote": return False, "not labelled remote"
    want = set(p["countries"])
    for r in p["regions"]: want |= MACRO.get(r, set())
    if not want: return None, "no country in preference"
    have = set(j["countries"])
    for r in j["regions"]: have |= MACRO.get(r, set())
    # restricting phrases in the JD text add countries/regions to `have`
    for m in RESTRICT_RE.finditer((jd or "")[:6000]):
        cs, rs = find_places(m.group(0))
        have |= cs
        for r in rs: have |= MACRO.get(r, set())
    if have & want: return True, "location matches"
    if have: return False, f"restricted to {', '.join(sorted(have))[:40]}"
    if j["remote"] == "remote": return True, "remote, no stated region"
    return None, "no location info"
