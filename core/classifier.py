import math
from typing import Optional

import nextcord
import numpy as np
from nextcord.ext.commands import Bot
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.model_selection import train_test_split
from sklearn.svm import SVC

from core.files import Config, Data, LinkedFaqEntry
from core.ui import AutoResponseView
import core.log as log


class Store:
    def __init__(self, bot: nextcord.ext.commands.Bot):
        self.classifiers: dict = {}
        self.config = Config()
        self.bot = bot

    def load_classifiers(self) -> None:
        self.classifiers = {}

        for topic in self.config.topics():
            AutoFaq(self.bot, topic, test_split=0.3, random_state=42)  # test score
            self.classifiers[topic] = AutoFaq(self.bot, topic)


class AutoFaq:
    def __init__(self, bot: Bot, topic: str, test_split: Optional[float] = None, min_threshold: float = 0.3,
                 max_threshold: float = 0.7, random_state: int = None):
        self.bot = bot
        self.topic = topic
        self.test_split = test_split
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold
        self.random_state = random_state

        self.data = Data(topic)
        self.classifier: Optional[SVC] = None
        self.vectorizer: Optional[CountVectorizer] = None

        self.__load__()

    def __load__(self) -> None:
        if not self.data.is_valid():
            return

        sentences_train, sentences_test, y_train, y_test, class_weights = self.__get_data__()
        X_train, X_test = self.__load_vectorizer__(sentences_train, sentences_test)
        self.__load_classifier__(X_train, y_train)

        if self.test_split:
            sample_weight = []
            for value in y_test:
                sample_weight.append(1 - class_weights[value])

            score = self.classifier.score(X_test, y_test, sample_weight=sample_weight)
            log.info(f"Classifier in topic {self.topic} loaded. Score:", round(score, 4))

    def __load_vectorizer__(self, sentences_train, sentences_test) -> (np.ndarray, np.ndarray):
        self.vectorizer = CountVectorizer()
        return self.vectorizer.fit_transform(sentences_train), self.vectorizer.transform(sentences_test)

    def __load_classifier__(self, X_train: np.ndarray, y_train: np.ndarray) -> None:
        self.classifier = SVC(probability=True, class_weight="balanced", random_state=self.random_state)
        self.classifier.fit(X_train, y_train)

    def __get_data__(self) -> (np.ndarray, np.ndarray, np.ndarray, np.ndarray):
        sentences = []
        y = []

        for text in self.data.nonsense():
            sentences.append(text)
            y.append(0)

        for entry in self.data.linked_faq():
            for message in entry.messages():
                sentences.append(message)
                y.append(entry.id + 1)

        class_weights = np.unique(y, return_counts=True)[1] / len(y)
        sentences_train, sentences_test, y_train, y_test = train_test_split(sentences, y, test_size=self.test_split,
                                                                            random_state=self.random_state,
                                                                            shuffle=True)
        return sentences_train, sentences_test, y_train, y_test, class_weights

    def refit(self) -> None:
        self.data = Data(self.topic)
        self.__load__()

    def predict(self, message: str) -> (Optional[int], int):
        if self.classifier is None:
            return None, None

        word_count = len(message.split(" "))
        if word_count < 3:
            return None, None

        message = self.data.clean_message(message)

        if len(message) == 0:
            return None, None

        vector = self.vectorizer.transform([message]).toarray()[0]

        p = self.classifier.predict_proba([vector])
        argmax = p.argmax()
        class_idx = argmax - 1 if argmax > 0 else None

        return class_idx, p.max()

    async def check_message(self, reply_on: nextcord.Message) -> (Optional[str], Optional[AutoResponseView]):
        answer_id, p = self.predict(reply_on.content)

        if answer_id is None:
            # message classified as nonsense
            log.info("Incoming message:", reply_on.content, "(nonsense" + (f", {round(p, 4)}" if p else "") + ")")
            return

        # change class index to answer_id
        entry = self.data.faq_entry(answer_id)
        threshold = self.__calculate_threshold__(answer_id)

        log.info("Incoming message:", reply_on.content,
                 f"({entry.short()}, p={round(p, 4)}, threshold={threshold}, {p >= threshold})")

        if p >= threshold:
            await self.send_faq(reply_on, answer_id, entry.answer(), True)

    async def send_faq(self, reply_on: nextcord.Message, answer_id: int, answer: str, allow_feedback: bool) -> None:
        if allow_feedback:
            view = AutoResponseView(reply_on.author, lambda vote: self.apply_vote(answer_id, vote))
            response = await reply_on.reply(answer, view=view)
            view.apply_context(response)
        else:
            await reply_on.reply(answer)

    def apply_vote(self, answer_id: int, vote: int) -> None:
        if vote > 0:
            self.data.faq_entry(answer_id).vote_up()
        elif vote < 0:
            self.data.faq_entry(answer_id).vote_down()

    def __calculate_threshold__(self, answer_id: int) -> Optional[float]:
        entry = self.data.faq_entry(answer_id)

        if entry.votes() == 0:
            return 0.5 * self.min_threshold + 0.5 * self.max_threshold

        # ratings gain importance over the default value until 10 votes are reached
        importance = min(math.log(entry.votes()), 1)

        ratio = importance * entry.up_votes() / entry.votes() + (1 - importance) * 0.5  # bad 0 - good 1
        return ratio * self.min_threshold + (1 - ratio) * self.max_threshold

    async def add_message_by_short(self, command: nextcord.Message, referenced: nextcord.Message,
                                   answer_abbreviation: str) -> None:
        content = self.data.clean_message(referenced.content)
        if len(content) == 0:
            await command.add_reaction("🤔")
            return

        answer_abbreviation = answer_abbreviation.lower()

        if answer_abbreviation == "ignore":
            self.data.add_nonsense(content)
            self.refit()

            log.info(f"The message '{referenced.content}' was added to the nonsense dataset",
                     f"by {command.author.name}#{command.author.discriminator}.")

            # delete last message which replied to the referenced message
            async for old in command.channel.history(limit=20):
                member: nextcord.Member = old.author
                if member.id == self.bot.user.id and old.reference:
                    if old.reference.message_id == referenced.id:
                        await old.delete()
                        break

            await command.add_reaction("✅")
            return

        message_id = 0
        entry = self.data.faq_entry_by_short(answer_abbreviation)

        if entry:
            if entry.add_message(content):
                log.info(f"The message '{referenced.content}' was added to the '{entry.short()}' dataset",
                         f"by {command.author.name}#{command.author.discriminator}.")
            await self.send_faq(referenced, message_id, entry.answer(), False)
            self.refit()
            return

        await command.add_reaction("🤔")

    async def create_answer(self, answer: str, short: str, interaction: nextcord.Interaction) -> bool:
        if short == "ignore":
            await interaction.send(f"The short *{short}* is reserved. Please choose another one.", ephemeral=True)
            return False

        entry: LinkedFaqEntry = self.data.faq_entry_by_short(short)
        if entry:
            await interaction.send(f"The short *{short}* is already registered. It's answer is *{entry.answer()}*",
                                   ephemeral=True)
            return False

        entry: LinkedFaqEntry = self.data.faq_entry_by_answer(answer)
        if entry:
            await interaction.send(f"The answer *{answer}* is already registered. It's short is *{entry.short()}*",
                                   ephemeral=True)
            return False

        self.data.add_faq_entry(answer, short)
        return True


store: Optional[Store] = None


def setup(s: Store) -> Store:
    global store
    store = s
    return s
