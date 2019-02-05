from django.db import models
from django.utils import timezone
from django.contrib.auth.models import AbstractUser
from django.urls import reverse


class User(AbstractUser):

    reputation = models.DecimalField(
        max_digits=6, decimal_places=4, blank=True, null=True)
    post_count = models.IntegerField(blank=True, null=True)
    sp = models.DecimalField(max_digits=64, decimal_places=4, blank=True,
                             null=True)
    account_age = models.IntegerField(blank=True, null=True)

    @property
    def polls_created(self):
        return Question.objects.filter(
            username=self.username).order_by('-id')

    @property
    def recent_questions(self):
        return self.polls_created[0:10]

    @property
    def votes_casted(self):
        return Choice.objects.filter(
            voted_users__username=self.username).order_by('-id')

    @property
    def recent_choices(self):
        return self.votes_casted[0:10]

    @property
    def total_polls_created(self):
        return self.polls_created.count()

    @property
    def total_votes_casted(self):
        return self.votes_casted.count()

    @property
    def profile_url(self):
        return reverse('profile', args=[self.username])


class Question(models.Model):
    text = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expire_at = models.DateTimeField('Expiration date')
    username = models.CharField(max_length=255)
    permlink = models.CharField(max_length=255, blank=True, null=True,
                                db_index=True)
    allow_multiple_choices = models.BooleanField(default=False)
    voter_count = models.IntegerField(default=0)
    promotion_amount = models.FloatField(
        blank=True,
        null=True,
        help_text="Promotion amount in SBD")

    def __str__(self):
        return self.text

    @property
    def expire_at_humanized(self):
        diff_in_days = (self.expire_at - self.created_at).days
        if diff_in_days <= 7:
            return "1_week"
        else:
            return "1_month"

    class Meta:
        unique_together = ('username', 'permlink')

    def is_votable(self):
        return self.expire_at > timezone.now()

    def is_editable(self):
        """
        Two rules to decide if a Poll is editable or not:
            - Poll must be open.
            - Poll must not have any votes casted from other users.
        """
        if not self.is_votable:
            return False
        votes = Choice.objects.filter(
            question=self).aggregate(votes=models.Count('voted_users'))
        return votes["votes"] == 0

    def update_voter_count(self):
        """
        Update a Question object's voter count with the registered voters.
        Discards multiple votes from the same vote caster.
        :return (Question): self
        """
        voters = []
        for choice in self.choices.all():
            voters += choice.voted_users.values_list('username', flat=True)
        self.voter_count = len(list(set(voters)))
        return self


class Choice(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE,
                                 related_name="choices")
    text = models.CharField(max_length=200)
    voted_users = models.ManyToManyField(User)

    @property
    def votes(self):
        return self.voted_users.all().count()

    def filtered_vote_count(self, rep, account_age, post_count, sp):
        filtered_user_count = 0
        for user in self.voted_users.all():
            if rep:
                try:
                    rep = int(rep)
                    if user.reputation < rep:
                        continue
                except ValueError:
                    pass

            if account_age:
                try:
                    account_age = int(account_age)
                    if user.account_age < account_age:
                        continue
                except ValueError:
                    pass
            if post_count:
                if isinstance(post_count, int):
                    if user.post_count < post_count:
                        continue
            if sp:
                try:
                    if user.sp < int(sp):
                        continue
                except ValueError:
                    pass
            filtered_user_count += 1

        return filtered_user_count

    def __str__(self):
        return self.text


class PromotionTransaction(models.Model):
    from_user = models.CharField(max_length=255)
    amount = models.FloatField()
    trx_id = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    memo = models.TextField(null=True, blank=True)
    author = models.CharField(max_length=255, null=True, blank=True)
    permlink = models.CharField(max_length=255, null=True, blank=True)


class VoteAudit(models.Model):
    """Stores the blockchain references of the votes casted on dPoll.
    """
    question = models.ForeignKey(Question, on_delete=models.DO_NOTHING)
    choices = models.ManyToManyField(Choice, blank=True)
    voter = models.ForeignKey(User, on_delete=models.DO_NOTHING)
    block_id = models.BigIntegerField(blank=True, null=True)
    trx_id = models.TextField(blank=True, null=True)
