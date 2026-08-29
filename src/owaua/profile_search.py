"""Semantic expansion and ranking for archived user-profile claims."""

from __future__ import annotations

import re

_LOCATION_QUESTION = re.compile(
    r"\b(?:nationality|citizenship|country of origin|what country|which country|"
    r"where\b.{0,40}\b(?:from|live|lives|living|born)|origin)\b",
    re.IGNORECASE,
)

# Country names and common English demonyms. Multi-word values are kept as FTS
# phrases. This is retrieval vocabulary, not a claim that the terms are
# interchangeable (for example, residence and nationality remain distinct).
_PROFILE_TERMS_TEXT = """
afghanistan,afghan,albania,albanian,algeria,algerian,andorra,andorran,angola,angolan,
argentina,argentinian,armenia,armenian,australia,australian,austria,austrian,
azerbaijan,azerbaijani,bahamas,bahamian,bahrain,bahraini,bangladesh,bangladeshi,
barbados,barbadian,belarus,belarusian,belgium,belgian,belize,belizean,benin,beninese,
bhutan,bhutanese,bolivia,bolivian,bosnia,bosnian,botswana,botswanan,brazil,brazilian,
britain,british,brunei,bruneian,bulgaria,bulgarian,burkina faso,burkinabe,burundi,
burundian,cambodia,cambodian,cameroon,cameroonian,canada,canadian,cape verde,
central african republic,chad,chadian,chile,chilean,china,chinese,colombia,colombian,
comoros,comorian,congo,congolese,costa rica,costa rican,croatia,croatian,cuba,cuban,
cyprus,cypriot,czechia,czech,denmark,danish,djibouti,djiboutian,dominica,dominican,
dominican republic,ecuador,ecuadorian,egypt,egyptian,el salvador,salvadoran,england,
english,equatorial guinea,equatorial guinean,eritrea,eritrean,estonia,estonian,
eswatini,swazi,ethiopia,ethiopian,fiji,fijian,finland,finnish,france,french,gabon,
gabonese,gambia,gambian,georgia,georgian,germany,german,ghana,ghanaian,greece,greek,
grenada,grenadian,guatemala,guatemalan,guinea,guinean,guyana,guyanese,haiti,haitian,
honduras,honduran,hungary,hungarian,iceland,icelandic,india,indian,indonesia,
indonesian,iran,iranian,persian,iraq,iraqi,ireland,irish,israel,israeli,italy,italian,
ivory coast,ivorian,jamaica,jamaican,japan,japanese,jordan,jordanian,kazakhstan,
kazakh,kenya,kenyan,kiribati,kuwait,kuwaiti,kyrgyzstan,kyrgyz,laos,lao,laotian,
latvia,latvian,lebanon,lebanese,lesotho,liberia,liberian,libya,libyan,liechtenstein,
lithuania,lithuanian,luxembourg,luxembourgish,madagascar,malagasy,malawi,malawian,
malaysia,malaysian,maldives,maldivian,mali,malian,malta,maltese,marshall islands,
marshallese,mauritania,mauritanian,mauritius,mauritian,mexico,mexican,micronesia,
micronesian,moldova,moldovan,monaco,monegasque,mongolia,mongolian,montenegro,
montenegrin,morocco,moroccan,mozambique,mozambican,myanmar,burmese,namibia,namibian,
nauru,nauruan,nepal,nepali,netherlands,dutch,new zealand,new zealander,nicaragua,
nicaraguan,niger,nigerien,nigeria,nigerian,north korea,north korean,north macedonia,
macedonian,norway,norwegian,oman,omani,pakistan,pakistani,palau,palauan,palestine,
palestinian,panama,panamanian,papua new guinea,papua new guinean,paraguay,
paraguayan,peru,peruvian,philippines,filipino,filipina,poland,polish,portugal,
portuguese,qatar,qatari,romania,romanian,russia,russian,rwanda,rwandan,samoa,samoan,
saudi arabia,saudi,scotland,scottish,senegal,senegalese,serbia,serbian,seychelles,
seychellois,sierra leone,sierra leonean,singapore,singaporean,slovakia,slovak,
slovenia,slovenian,solomon islands,solomon islander,somalia,somali,south africa,
south african,south korea,south korean,spain,spanish,sri lanka,sri lankan,sudan,
sudanese,suriname,surinamese,sweden,swedish,switzerland,swiss,syria,syrian,taiwan,
taiwanese,tajikistan,tajik,tanzania,tanzanian,thailand,thai,timor leste,timorese,
togo,togolese,tonga,tongan,trinidad and tobago,trinidadian,tunisia,tunisian,turkey,
turkish,turkmenistan,turkmen,tuvalu,tuvaluan,uganda,ugandan,ukraine,ukrainian,
united arab emirates,emirati,united kingdom,united states,america,american,uruguay,
uruguayan,uzbekistan,uzbek,vanuatu,vanuatuan,vatican,venezuela,venezuelan,vietnam,
vietnamese,wales,welsh,yemen,yemeni,zambia,zambian,zimbabwe,zimbabwean
"""

PROFILE_TERMS = tuple(
    dict.fromkeys(
        value.strip() for value in _PROFILE_TERMS_TEXT.replace("\n", "").split(",") if value.strip()
    )
)
_TERM_PATTERN = "|".join(
    sorted((re.escape(value) for value in PROFILE_TERMS), key=len, reverse=True)
)
_TERM_RE = re.compile(rf"(?<![a-z])(?:{_TERM_PATTERN})(?![a-z])", re.IGNORECASE)
_DIRECT_CLAIM_RE = re.compile(
    rf"\b(?:i(?:\s+am|['’]?m)|we(?:\s+are|['’]?re))\s+(?:an?\s+)?"
    rf"(?:{_TERM_PATTERN})(?![a-z])|"
    rf"\b(?:from|live\s+in|living\s+in|born\s+in)\s+(?:the\s+)?"
    rf"(?:{_TERM_PATTERN})(?![a-z])|"
    rf"\b(?:{_TERM_PATTERN})\s+(?:immigrant|citizen|nationality)\b",
    re.IGNORECASE,
)
_NEGATED_RE = re.compile(
    rf"\b(?:not|never)\s+(?:an?\s+)?(?:{_TERM_PATTERN})(?![a-z])",
    re.IGNORECASE,
)


def is_location_question(question: str) -> bool:
    return bool(_LOCATION_QUESTION.search(str(question or "")))


def claim_score(content: str) -> int:
    """Rank direct/short country claims above incidental geographic mentions."""
    value = " ".join(str(content or "").lower().split())
    if not value or not _TERM_RE.search(value):
        return 0
    score = 20
    if _DIRECT_CLAIM_RE.search(value):
        score += 70
    if _NEGATED_RE.search(value):
        score -= 80
    if _TERM_RE.fullmatch(value):
        score += 100
    if len(value) <= 40:
        score += 20
    return score
