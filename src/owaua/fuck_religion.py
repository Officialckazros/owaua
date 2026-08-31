"""Seed the knowledge base with operator-reviewed religion content."""

import argparse
import os

from owaua import kb
from owaua.scope import Scope

CORPUS = {
    "Religion (overview)": """
Religion is a system of beliefs, practices, ethics, and often institutions that
relate humanity to what it regards as sacred, transcendent, or ultimately real.
Scholars distinguish several dimensions common across religions: belief/doctrine,
ritual, ethics, narrative/myth, experience, community/social organization, and
material culture (art, architecture, sacred objects).

The largest religions by number of adherents are Christianity (~2.4 billion),
Islam (~1.9 billion), Hinduism (~1.2 billion), and Buddhism (~0.5 billion),
followed by folk and traditional religions and a large number of religiously
unaffiliated people. Religions are often grouped into families: the Abrahamic
religions (Judaism, Christianity, Islam), the Indian or Dharmic religions
(Hinduism, Buddhism, Jainism, Sikhism), the East Asian traditions (Taoism,
Confucianism, Shinto), and numerous indigenous and new religious movements.

Key comparative terms: monotheism (belief in one God), polytheism (many gods),
pantheism (the divine is identical with the universe), nontheism (no creator
god, e.g. much of Buddhism), theism vs. deism, orthodoxy (right belief) vs.
orthopraxy (right practice), and the distinction between the sacred and the
profane. Theodicy is the problem of reconciling evil and suffering with a good,
powerful God; soteriology is the study of salvation or liberation; eschatology
concerns final things — death, judgment, and the destiny of the world.
""",
    "Judaism": """
Judaism is the monotheistic religion of the Jewish people, tracing its covenant
with God to the patriarch Abraham and the revelation of the Torah to Moses at
Mount Sinai. Its foundational text is the Tanakh (the Hebrew Bible), comprising
the Torah (Five Books of Moses), Nevi'im (Prophets), and Ketuvim (Writings).
Rabbinic tradition is recorded in the Talmud (Mishnah plus Gemara), the central
text of Jewish law (halakha) and lore (aggadah).

Core beliefs include the oneness of God (declared in the Shema, "Hear, O Israel:
the Lord our God, the Lord is one"), the covenant between God and Israel, and the
importance of ethical and ritual commandments (mitzvot; tradition counts 613).
Major observances: Shabbat (the weekly day of rest from Friday to Saturday
evening), Rosh Hashanah (new year), Yom Kippur (Day of Atonement), Passover
(Pesach, marking the Exodus from Egypt), Shavuot, Sukkot, and Hanukkah. Life-cycle
rites include brit milah (circumcision) and bar/bat mitzvah.

Major movements today include Orthodox, Conservative (Masorti), Reform, and
Reconstructionist Judaism, differing on how binding traditional law is and how it
adapts to modernity. The synagogue is the house of worship; a rabbi is a teacher
and legal authority. Jerusalem, and specifically the Western Wall (remnant of the
Second Temple), is the holiest site in Jewish practice.
""",
    "Christianity": """
Christianity is a monotheistic, Abrahamic religion centered on the life, death,
and resurrection of Jesus of Nazareth, whom Christians confess as the Christ (the
Messiah) and the incarnate Son of God. Its scripture is the Bible: the Old
Testament (largely shared with the Hebrew Bible) and the New Testament (the four
Gospels, Acts, the Epistles, and Revelation). Central doctrines for most
Christians include the Trinity (one God in three persons: Father, Son, and Holy
Spirit), the incarnation, atonement through Jesus' crucifixion, and salvation by
grace.

Christianity has three major branches. Roman Catholicism, led by the Pope in
Rome, is the largest. Eastern Orthodoxy comprises self-governing churches
(e.g. Greek, Russian) that split from Rome in the Great Schism of 1054.
Protestantism arose in the 16th-century Reformation (Martin Luther, John Calvin)
and includes Lutheran, Reformed, Anglican, Baptist, Methodist, Pentecostal, and
many other denominations, generally emphasizing scripture (sola scriptura) and
grace (sola gratia).

Common practices include baptism and the Eucharist (Communion / Lord's Supper),
prayer, and worship on Sunday. The principal feasts are Christmas (the nativity
of Jesus, December 25 in most churches) and Easter (his resurrection), preceded
by Advent and Lent respectively. Pentecost celebrates the descent of the Holy
Spirit. The Nicene and Apostles' Creeds summarize shared belief.
""",
    "Islam": """
Islam is a monotheistic, Abrahamic religion teaching submission (islam) to the
one God (Allah) as revealed to the Prophet Muhammad (c. 570–632 CE) in Arabia.
Its scripture is the Qur'an, believed to be God's literal word revealed through
the angel Gabriel; the Sunnah (the Prophet's example, recorded in hadith)
supplements it. Muhammad is regarded as the last in a line of prophets that
includes Abraham, Moses, and Jesus.

Practice centers on the Five Pillars: the shahada (declaration that there is no
god but God and Muhammad is his messenger), salat (five daily prayers facing
Mecca), zakat (obligatory almsgiving), sawm (fasting during the month of
Ramadan), and hajj (pilgrimage to Mecca at least once if able). The Kaaba in
Mecca is the holiest site; Medina and Jerusalem follow.

The two largest branches are Sunni Islam (the majority, ~85–90%) and Shia Islam,
which diverged over the question of legitimate succession to Muhammad — Sunnis
accepting the early caliphs, Shia holding that leadership belonged to Ali and his
descendants (the Imams). Sharia is Islamic law derived from the Qur'an, Sunnah,
consensus, and analogical reasoning. Major festivals are Eid al-Fitr (ending
Ramadan) and Eid al-Adha (the feast of sacrifice, during hajj). Sufism is Islam's
mystical, devotional tradition.
""",
    "Hinduism": """
Hinduism is the oldest major living religious tradition, originating in the
Indian subcontinent. Rather than a single founder or creed, it is a diverse
family of beliefs and practices. Core concepts shared across most schools include
Brahman (the ultimate reality), atman (the self or soul), dharma (duty, ethical
order), karma (moral cause and effect), samsara (the cycle of rebirth), and moksha
(liberation from that cycle). Many Hindus worship one supreme reality expressed
through many deities.

Principal deities include Brahma (creator), Vishnu (preserver, with avatars such
as Rama and Krishna), and Shiva (transformer), along with the Goddess (Devi/Shakti)
in forms like Durga, Kali, and Lakshmi, and Ganesha. Sacred texts fall into two
categories: shruti ("that which is heard" — the Vedas and Upanishads) and smriti
("that which is remembered" — the epics Ramayana and Mahabharata, the latter
containing the Bhagavad Gita, plus the Puranas).

Practices include puja (worship), meditation and yoga, pilgrimage (e.g. to the
Ganges at Varanasi and the Kumbh Mela), and observance of numerous festivals such
as Diwali (festival of lights), Holi (festival of colors), Navaratri, and Durga
Puja. Philosophical schools range from Advaita Vedanta (non-dualism) to devotional
(bhakti) movements. The traditional social system of varna and jati (caste) has
been historically influential and is now widely contested and legally challenged.
""",
    "Buddhism": """
Buddhism is a nontheistic Indian tradition founded by Siddhartha Gautama, the
Buddha ("awakened one"), around the 5th century BCE. Its aim is liberation from
suffering and the cycle of rebirth (samsara) through awakening (nirvana). The core
teaching is the Four Noble Truths: (1) life involves suffering/unsatisfactoriness
(dukkha); (2) suffering arises from craving and attachment; (3) suffering can
cease; (4) the path to cessation is the Noble Eightfold Path — right view,
intention, speech, action, livelihood, effort, mindfulness, and concentration.

Key ideas include anatta (no permanent self), anicca (impermanence), karma, and
dependent origination. Ethical life, meditation, and wisdom form the threefold
training. The Buddha, the Dharma (his teaching), and the Sangha (community) are
the "Three Jewels" in which Buddhists take refuge.

Major traditions: Theravada ("the way of the elders"), dominant in Sri Lanka and
Southeast Asia, emphasizing the Pali Canon and monastic practice; Mahayana,
prevalent in East Asia, introducing the bodhisattva ideal (postponing one's own
nirvana to help others) and schools such as Zen/Chan, Pure Land, and Tiantai; and
Vajrayana or Tibetan Buddhism, which adds tantric practice and figures such as the
Dalai Lama. Vesak commemorates the Buddha's birth, enlightenment, and death.
""",
    "Sikhism": """
Sikhism is a monotheistic religion founded in the Punjab region of South Asia in
the late 15th century by Guru Nanak. Sikhs believe in one formless God (Ik Onkar)
and follow the teachings of ten human Gurus, culminating in the Guru Granth Sahib,
the scripture that serves as the eternal living Guru. Central values are honest
work, sharing with others, and remembrance of God through meditation on the divine
name (naam simran).

Guru Nanak taught the equality of all people regardless of caste, gender, or creed,
and rejected ritualism and idolatry. The tenth Guru, Gobind Singh, founded the
Khalsa in 1699, a community of initiated Sikhs who observe the Five Ks: kesh
(uncut hair, usually under a turban), kara (a steel bracelet), kanga (a wooden
comb), kachera (cotton undergarments), and kirpan (a small ceremonial sword).

The gurdwara is the Sikh place of worship; every gurdwara runs a langar, a free
community kitchen serving all visitors equally, expressing the principle of seva
(selfless service). The Harmandir Sahib (Golden Temple) in Amritsar is the holiest
gurdwara. Major observances include Vaisakhi (marking the founding of the Khalsa)
and Gurpurbs (anniversaries of the Gurus).
""",
    "Jainism": """
Jainism is an ancient Indian religion emphasizing non-violence, non-attachment,
and self-discipline as the path to liberation of the soul (moksha) from the cycle
of rebirth. It reveres a succession of twenty-four Tirthankaras ("ford-makers"),
spiritual teachers who achieved liberation; the last, Mahavira (c. 599–527 BCE),
is often regarded as a contemporary of the Buddha and a systematizer of the faith.

Its defining principle is ahimsa (non-violence) toward all living beings, taken to
a rigorous degree — many Jains are strict vegetarians and take great care to avoid
harming even small creatures. Other core vows are truthfulness (satya),
non-stealing (asteya), chastity (brahmacharya), and non-possessiveness
(aparigraha). The doctrine of anekantavada ("many-sidedness") holds that truth is
complex and can be seen from multiple valid perspectives.

Jainism is nontheistic in the sense that it posits no creator god; liberation is
achieved through one's own ethical and ascetic effort. Its two main sects are the
Digambara ("sky-clad," whose most advanced monks renounce clothing) and Svetambara
("white-clad"). Paryushana is its most important festival, a period of fasting,
reflection, and forgiveness.
""",
    "Taoism and Confucianism": """
Taoism (Daoism) and Confucianism are the two great indigenous philosophical and
religious traditions of China, both emerging in the "Hundred Schools" period
around the 6th–5th centuries BCE.

Taoism, associated with the legendary Laozi and the text Tao Te Ching (Daodejing),
centers on the Tao ("the Way"), the ineffable source and pattern of the universe.
It prizes wu wei (effortless, non-forcing action), naturalness, simplicity, and
harmony with the flow of nature, and the balance of complementary forces yin and
yang. Religious Taoism developed deities, rituals, alchemy, and practices aimed at
health and longevity, alongside the philosophical strand of the Zhuangzi.

Confucianism, founded on the teachings of Confucius (Kongzi, 551–479 BCE) and
recorded in the Analects, is primarily an ethical and social philosophy. It
emphasizes ren (benevolence/humaneness), li (ritual propriety and etiquette), xiao
(filial piety), righteousness, and the cultivation of virtue to create a
harmonious society and well-ordered state. Its ideal is the junzi, the
"exemplary person." Later thinkers such as Mencius and Xunzi, and the
Neo-Confucian revival, extended the tradition, which profoundly shaped East Asian
government, education, and family life.
""",
    "Shinto": """
Shinto ("the way of the kami") is the indigenous religion of Japan, an ancient set
of practices oriented toward the kami — sacred spirits or presences found in
nature (mountains, rivers, trees), in notable ancestors, and in deities of myth.
It has no single founder, no fixed scripture, and no strong doctrinal creed;
rather, it emphasizes ritual purity, gratitude, and living in harmony with the
kami and the natural world.

Worship centers on shrines (jinja), marked by torii gates, where visitors purify
themselves with water, offer prayers, and make offerings. Priests perform rituals
(matsuri, festivals) to honor and petition the kami. Concepts of purity and
pollution are central: purification rites (harae) restore ritual cleanliness.

Shinto has coexisted and blended with Buddhism in Japan for over a thousand years,
and many Japanese participate in both — for example, Shinto rites for births and
weddings and Buddhist rites for funerals. The mythological chronicles Kojiki and
Nihon Shoki record its creation myths, including the sun goddess Amaterasu, from
whom the imperial line was traditionally said to descend.
""",
    "Bahá'í Faith": """
The Bahá'í Faith is a monotheistic religion founded in 19th-century Persia by
Bahá'u'lláh (1817–1892), whom Bahá'ís regard as the most recent in a series of
divine messengers that includes Abraham, Krishna, Moses, Zoroaster, the Buddha,
Jesus, and Muhammad. Its central teaching is the essential unity of God, of
religion, and of humanity: the world's major faiths are seen as successive
chapters of one evolving revelation ("progressive revelation").

Bahá'ís emphasize the oneness of humankind, the elimination of prejudice, the
equality of men and women, the harmony of science and religion, universal
education, and the establishment of world peace through global cooperation. The
faith has no clergy; communities are administered by elected councils (Spiritual
Assemblies) and, internationally, by the Universal House of Justice, based in
Haifa, Israel, near the Shrine of the Báb.

Practices include daily prayer, an annual nineteen-day fast, and gatherings on the
first day of each month of the Bahá'í calendar (which has nineteen months of
nineteen days). Bahá'u'lláh's writings, such as the Kitáb-i-Aqdas, are its
scripture. The Báb, a herald who prepared the way for Bahá'u'lláh, is a central
figure in its origins.
""",
    "Zoroastrianism": """
Zoroastrianism is one of the world's oldest continuously practiced religions,
founded by the prophet Zoroaster (Zarathustra) in ancient Persia, likely in the
2nd millennium BCE. It is centered on the worship of Ahura Mazda, the wise creator
god, and frames existence as a cosmic struggle between truth/order (asha) and
falsehood/chaos (druj), with the hostile spirit Angra Mainyu (Ahriman) opposing
the good.

Its ethical core is captured in the maxim "good thoughts, good words, good deeds."
Human beings have free will and a responsibility to side with truth. Fire is
venerated as a symbol of purity and the divine presence, and worship takes place
in fire temples. The scripture is the Avesta, whose oldest layer, the Gathas, is
attributed to Zoroaster himself.

Zoroastrianism was the state religion of successive Persian empires until the
Muslim conquest of the 7th century CE, after which it declined. Today its adherents
are relatively few, concentrated among the Parsi community of India and in Iran.
Its concepts of a cosmic dualism, judgment after death, heaven and hell, a final
renovation of the world, and a coming savior are often noted for their possible
influence on later Abrahamic eschatology.
""",
    "Indigenous and folk religions": """
Beyond the large world religions, hundreds of millions of people practice
indigenous, traditional, and folk religions rooted in specific peoples and places.
These include African traditional religions (such as Yoruba religion and its
diaspora forms like Santería and Candomblé), Native American and First Nations
traditions, Aboriginal Australian belief in the Dreaming, Chinese and other Asian
folk religions, and countless local traditions.

Common features often include animism (the attribution of spirit or life to
natural phenomena and objects), reverence for ancestors, oral rather than written
transmission, sacred landscapes, and ritual specialists such as shamans or
priests who mediate between the human and spirit worlds. These traditions are
frequently tied closely to community identity, agriculture, seasonal cycles, and
rites of passage.

Many folk practices blend or coexist with major religions (syncretism) — for
example, Chinese popular religion mixing Taoist, Buddhist, and Confucian elements,
or Latin American Catholicism incorporating indigenous customs. New religious
movements — including modern Paganism, Wicca, and numerous others — also continue
to emerge, illustrating that the religious landscape is dynamic rather than fixed.
""",
    "Sacred texts and concepts": """
Sacred texts anchor most religious traditions. Judaism has the Tanakh and Talmud;
Christianity the Bible (Old and New Testaments); Islam the Qur'an and hadith
collections; Hinduism the Vedas, Upanishads, Bhagavad Gita, and epics; Buddhism the
Pali Canon (Tripitaka) and Mahayana sutras; Sikhism the Guru Granth Sahib; Taoism
the Tao Te Ching and Zhuangzi; Confucianism the Analects and Five Classics;
Zoroastrianism the Avesta; and the Bahá'í Faith the writings of Bahá'u'lláh.

Cross-cutting religious concepts include: the sacred vs. the profane; myth
(foundational sacred narrative, not "falsehood"); ritual and rites of passage
(birth, initiation, marriage, death); prayer, meditation, and pilgrimage;
prophecy and revelation; priesthood and monasticism; the afterlife (heaven, hell,
rebirth, or ancestral existence); and moral codes such as the Ten Commandments,
the Five Precepts, or the Golden Rule ("treat others as you would be treated"),
versions of which appear across many traditions.

The academic study of religion (religious studies) approaches these traditions
comparatively and descriptively, distinct from theology (reasoning from within a
faith). Related fields include the sociology, psychology, anthropology, and history
of religion, and the philosophy of religion, which examines arguments about the
existence of God, the problem of evil, faith and reason, and religious language.
""",
}


def seed_corpus(scope_id: str) -> int:
    total = 0
    for topic, text in CORPUS.items():
        n = kb.ingest(text, topic=topic, title=topic, source="starter-corpus", scope_id=scope_id)
        print(f"  + {topic}: {n} passage(s)")
        total += n
    return total


def seed_folder(path: str, scope_id: str) -> int:
    exts = (".md", ".markdown", ".txt")
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in sorted(files):
            if not fn.lower().endswith(exts):
                continue
            fp = os.path.join(root, fn)
            try:
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError as e:
                print(f"  ! skip {fp}: {e}")
                continue
            topic = os.path.splitext(fn)[0]
            n = kb.ingest(text, topic=topic, title=topic, source=f"file:{fp}", scope_id=scope_id)
            print(f"  + {fp} → topic '{topic}': {n} passage(s)")
            total += n
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed one exact guild knowledge base")
    parser.add_argument("--guild-id", required=True, type=int, help="target Discord guild id")
    parser.add_argument("folder", nargs="?", help="optional folder of UTF-8 .md/.txt files")
    args = parser.parse_args()
    scope_id = Scope.guild(args.guild_id).key
    kb.ensure()
    before = kb.count(scope_id)
    print(f"knowledge base has {before} passage(s) before seeding.\n")

    print("loading built-in religion starter corpus…")
    total = seed_corpus(scope_id)

    if args.folder:
        folder = args.folder
        if os.path.isdir(folder):
            print(f"\ningesting files from {folder!r}…")
            total += seed_folder(folder, scope_id)
        else:
            print(f"\n! {folder!r} is not a directory — skipping folder ingest.")

    print(f"\ndone. added {total} passage(s); knowledge base now has {kb.count(scope_id)}.")
    print("topics:")
    for t in kb.topics(scope_id):
        print(f"  - {t['topic']}: {t['passages']}")


if __name__ == "__main__":
    main()
