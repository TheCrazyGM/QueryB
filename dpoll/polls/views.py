import copy
import uuid
import json
from datetime import timedelta

from dateutil.parser import parse
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login
from django.contrib.auth.views import auth_logout
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import Http404
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.csrf import csrf_exempt
from django.utils.timezone import now
from steemconnect.client import Client
from steemconnect.operations import Comment

from base.utils import add_tz_info
from .models import Question, Choice, User, VoteAudit
from communities.models import Community

from .utils import (
    get_sc_client, get_comment_options, get_top_dpollers,
    get_top_voters, validate_input, add_or_get_question, add_choices,
    get_comment, fetch_poll_data, sanitize_filter_value)

from lightsteem.client import Client as LightsteemClient


TEAM_MEMBERS = [
        {
            "username": "emrebeyler",
            "title": "Developer",
        },
        {
            "username": "isnochys",
            "title": "Joker",
        },
        {
            "username": "bluerobo",
            "title": "Curator",
        },
        {
            "username": "tolgahanuzun",
            "title": "Developer",
        }
    ]


def index(request):

    query_params = {
        "expire_at__gt": now(),
        "is_deleted": False,
    }
    # ordering by new, trending, or promoted.
    order_by = "-id"
    if request.GET.get("order"):
        if request.GET.get("order") == "trending":
            order_by = "-voter_count"
        elif request.GET.get("order") == "promoted":
            order_by = "-promotion_amount"
            query_params.update({
                "promotion_amount__gt": float(0.000),
            })

    questions = Question.objects.filter(**query_params).order_by(order_by)
    paginator = Paginator(questions, 10)

    promoted_polls = Question.objects.filter(
        expire_at__gt=now(),
        promotion_amount__gt=float(0.000),
    ).order_by("-promotion_amount")

    if len(promoted_polls):
        promoted_poll = promoted_polls[0]
    else:
        promoted_poll = None

    page = request.GET.get('page')
    polls = paginator.get_page(page)

    stats = {
        'poll_count': Question.objects.all().count(),
        'vote_count': Choice.objects.aggregate(
            total_votes=Count('voted_users'))["total_votes"],
        'user_count': User.objects.all().count(),
        'top_dpollers': get_top_dpollers(),
        'top_voters': get_top_voters(),
    }

    return render(request, "index.html", {
        "polls": polls, "stats": stats, "promoted_poll": promoted_poll})


def sc_login(request):
    if 'access_token' not in request.GET:
        login_url = get_sc_client().get_login_url(
            redirect_uri=settings.SC_REDIRECT_URI,
            scope="login,comment,comment_options",
        )
        return redirect(login_url)

    user = authenticate(access_token=request.GET.get("access_token"))

    if user is not None:
        if user.is_active:
            login(request, user)
            try:
                # Trigger update on user info (SP, rep, etc.)
                user.update_info()
            except Exception as e:
                user.update_info_async()
            request.session["sc_token"] = request.GET.get("access_token")
            if request.session.get("initial_referer"):
                return redirect(request.session["initial_referer"])
            return redirect("/")
        else:
            return HttpResponse("Account is disabled.")
    else:
        return HttpResponse("Invalid login details.")


def sc_logout(request):
    auth_logout(request)
    return redirect("/")


def create_poll(request):
    if not request.user.is_authenticated:
        return redirect('login')

    if request.method == 'POST':
        form_data = copy.copy(request.POST)

        if 'sc_token' not in request.session:
            return redirect("/")

        error, question, choices, expire_at, permlink, days, tags, \
            allow_multiple_choices = validate_input(request)

        if error:
            form_data.update({
                "answers": request.POST.getlist("answers[]"),
                "expire_at": request.POST.get("expire-at"),
                "reward_option": request.POST.get("reward-option"),
                "allow_multiple_choices": request.POST.get(
                    "allow-multiple-choices"),
            })
            return render(request, "add.html", {"form_data": form_data})

        if (Question.objects.filter(
                permlink=permlink, username=request.user)).exists():
            messages.add_message(
                request,
                messages.ERROR,
                "You have already a similar poll."
            )
            return redirect('create-poll')

        # add question
        question = add_or_get_question(
            request,
            question,
            permlink,
            days,
            allow_multiple_choices
        )
        question.save()

        # add answers attached to it
        add_choices(question, choices)

        # send it to the steem blockchain
        sc_client = Client(access_token=request.session.get("sc_token"), oauth_base_url="https://hivesigner.com/oauth2/", sc2_api_base_url="https://hivesigner.com/api/")
        comment = get_comment(request, question, choices, permlink, tags)
        comment_options = get_comment_options(
            comment,
            reward_option=request.POST.get("reward-option")
        )
        if not settings.BROADCAST_TO_BLOCKCHAIN:
            resp = {}
        else:
            resp = sc_client.broadcast([
                comment.to_operation_structure(),
                comment_options.to_operation_structure(),
            ])

        if 'error' in resp:
            if 'The token has invalid role' in resp.get("error_description"):
                # expired token
                auth_logout(request)
                return redirect('login')

            messages.add_message(
                request,
                messages.ERROR,
                resp.get("error_description", "error")
            )
            question.delete()
            return redirect('create-poll')

        return redirect('detail', question.username, question.permlink)

    return render(request, "add.html")


def edit_poll(request, author, permlink):
    if not request.user.is_authenticated:
        return redirect('login')

    try:
        poll = Question.objects.get(
            permlink=permlink,
            username=author,
        )
    except Question.DoesNotExist:
        raise Http404

    if author != request.user.username:
        raise Http404

    if request.method == "GET":
        poll_data = fetch_poll_data(poll.username, poll.permlink)
        tags = poll_data.get("tags", [])
        tags = [tag for tag in tags if tag not in settings.DEFAULT_TAGS]
        form_data = {
            "question": poll.text,
            "description": poll.description,
            "answers": [c.text for c in Choice.objects.filter(question=poll)],
            "expire_at": poll.expire_at_humanized,
            "tags": ",".join(tags),
            "allow_multiple_choices": poll.allow_multiple_choices
        }

    if request.method == 'POST':
        form_data = copy.copy(request.POST)

        if 'sc_token' not in request.session:
            return redirect("/")

        error, question, choices, expire_at, _, days, tags, \
            allow_multiple_choices = validate_input(request)
        if tags:
            tags = settings.DEFAULT_TAGS + tags
        else:
            tags = settings.DEFAULT_TAGS

        permlink = poll.permlink

        if error:
            form_data.update({
                "answers": request.POST.getlist("answers[]"),
                "expire_at": request.POST.get("expire-at"),
                "allow_multiple_choices": request.POST.get(
                    "allow-multiple-choices"),
            })
            return render(request, "edit.html", {"form_data": form_data})

        # add question
        question = add_or_get_question(
            request,
            question,
            permlink,
            days,
            allow_multiple_choices
        )
        question.save()

        # add answers attached to it
        add_choices(question, choices, flush=True)

        # send it to the steem blockchain
        sc_client = Client(access_token=request.session.get("sc_token"), oauth_base_url="https://hivesigner.com/oauth2/", sc2_api_base_url="https://hivesigner.com/api/")
        comment = get_comment(request, question, choices, permlink, tags=tags)
        if not settings.BROADCAST_TO_BLOCKCHAIN:
            resp = {}
        else:
            resp = sc_client.broadcast([
                comment.to_operation_structure(),
            ])

        if 'error' in resp:
            if 'The token has invalid role' in resp.get("error_description"):
                # expired token
                auth_logout(request)
                return redirect('login')

            messages.add_message(
                request,
                messages.ERROR,
                resp.get("error_description", "error")
            )
            question.delete()
            return redirect('edit', args=(author, permlink))

        return redirect('detail', question.username, question.permlink)

    return render(request, "edit.html", {
        "form_data": form_data,
    })


def detail(request, user, permlink):

    if 'after_promotion' in request.GET:
        messages.add_message(
            request,
            messages.SUCCESS,
            "Thanks for the promotion. Transfer will be picked up by our "
            "systems between 2 and 5 minutes."
        )

    try:
        poll = Question.objects.get(
            username=user, permlink=permlink, is_deleted=False)
    except Question.DoesNotExist:
        raise Http404

    rep = sanitize_filter_value(request.GET.get("rep"))
    sp = sanitize_filter_value(request.GET.get("sp"))
    age = sanitize_filter_value(request.GET.get("age"))
    post_count = sanitize_filter_value(request.GET.get("post_count"))
    community = request.GET.get("community")

    # check the existance of the community
    try:
        Community.objects.get(name=community)
    except Community.DoesNotExist:
        community = None

    if community:
        messages.add_message(
            request,
            messages.INFO,
            f"Note: Only showing {community} members' choices."
        )

    choice_list, choice_list_ordered, choices_selected, filter_exists, \
            all_votes = poll.votes_summary(
                age=age,
                rep=rep,
                sp=sp,
                post_count=post_count,
                stake_based=request.GET.get("stake_based") == "1",
                sa_stake_based=request.GET.get("stake_based") == "2",
                community=community,
            )

    user_votes = Choice.objects.filter(
        voted_users__username=request.user.username,
        question=poll,
    ).values_list('id', flat=True)

    if 'audit' in request.GET:
        return poll.audit_response(choice_list)

    return render(request, "poll_detail.html", {
        "poll": poll,
        "choices": choice_list,
        "choices_ordered": choice_list_ordered,
        "total_votes": all_votes,
        "user_votes": user_votes,
        "show_bars": choices_selected > 1,
        "filters_applied": filter_exists,
        "communities": Community.objects.all().order_by("-id"),
    })


def vote(request, user, permlink):
    if request.method != "POST":
        raise Http404

    # django admin users should not be able to vote.
    if not request.session.get("sc_token"):
        redirect('logout')

    try:
        poll = Question.objects.get(username=user, permlink=permlink)
    except Question.DoesNotExist:
        raise Http404

    if not request.user.is_authenticated:
        return redirect('login')

    if poll.allow_multiple_choices:
        choice_ids = request.POST.getlist("choice-id")
    else:
        choice_ids = [request.POST.get("choice-id"),]

    # remove noise
    choice_ids = [x for x in choice_ids if x is not None]

    additional_thoughts = request.POST.get("vote-comment", "")

    if not len(choice_ids):
        messages.add_message(
            request,
            messages.ERROR,
            "You need to pick a choice to vote."
        )
        return redirect("detail", poll.username, poll.permlink)

    if Choice.objects.filter(
            voted_users__username=request.user,
            question=poll).exists():
        messages.add_message(
            request,
            messages.ERROR,
            "You have already voted for this poll!"
        )

        return redirect("detail", poll.username, poll.permlink)

    if not poll.is_votable():
        messages.add_message(
            request,
            messages.ERROR,
            "This poll is expired!"
        )
        return redirect("detail", poll.username, poll.permlink)

    for choice_id in choice_ids:
        try:
            choice = Choice.objects.get(pk=int(choice_id))
        except Choice.DoesNotExist:
            raise Http404

    choice_instances = []
    for choice_id in choice_ids:
        choice = Choice.objects.get(pk=int(choice_id))
        choice_instances.append(choice)

    # send it to the steem blockchain
    sc_client = Client(access_token=request.session.get("sc_token"), oauth_base_url="https://hivesigner.com/oauth2/", sc2_api_base_url="https://hivesigner.com/api/")

    choice_text = ""
    for c in choice_instances:
        choice_text += f" - {c.text.strip()}\n"

    body = f"Voted for \n {choice_text}"
    if additional_thoughts:
        body += f"\n\n{additional_thoughts}"
    comment = Comment(
        author=request.user.username,
        permlink=str(uuid.uuid4()),
        body=body,
        parent_author=poll.username,
        parent_permlink=poll.permlink,
        json_metadata={
            "tags": settings.DEFAULT_TAGS,
            "app": f"dpoll/{settings.DPOLL_APP_VERSION}",
            "content_type": "poll_vote",
            "votes": [c.text.strip() for c in choice_instances],
        }
    )

    comment_options = get_comment_options(comment)
    if not settings.BROADCAST_TO_BLOCKCHAIN:
        resp = {}
    else:
        resp = sc_client.broadcast([
            comment.to_operation_structure(),
            comment_options.to_operation_structure(),
        ])

    # Steemconnect sometimes returns 503.
    # https://github.com/steemscript/steemconnect/issues/356
    if not isinstance(resp, dict):
        messages.add_message(
            request,
            messages.ERROR,
            "We got an unexpected error from Steemconnect. Please, try again."
        )
        return redirect("detail", poll.username, poll.permlink)

    # Expected way to receive errors on broadcasting
    if 'error' in resp:
        messages.add_message(
            request,
            messages.ERROR,
            resp.get("error_description", "error")
        )

        return redirect("detail", poll.username, poll.permlink)

    # register the vote to the database
    for choice_instance in choice_instances:
        choice_instance.voted_users.add(request.user)

    block_id = resp.get("result", {}).get("block_num")
    trx_id = resp.get("result", {}).get("id")

    # add trx id and block id to the audit log
    vote_audit = VoteAudit(
        question=poll,
        voter=request.user,
        block_id=block_id,
        trx_id=trx_id
    )
    vote_audit.save()
    for choice_instance in choice_instances:
        vote_audit.choices.add(choice_instance)

    messages.add_message(
        request,
        messages.SUCCESS,
        "You have successfully voted!"
    )

    return redirect("detail", poll.username, poll.permlink)


def profile(request, user):
    try:
        user = User.objects.get(username=user)
    except User.DoesNotExist:
        raise Http404

    polls = user.polls_created
    votes = user.votes_casted
    poll_count = len(polls)
    vote_count = len(votes)

    return render(request, "profile.html", {
        "user": user,
        "polls": polls,
        "votes": votes,
        "poll_count": poll_count,
        "vote_count": vote_count,
    })


def team(request):
    return render(request, "team.html", {"team_members": TEAM_MEMBERS})


def polls_by_vote_count(request):
    end_time = now()
    start_time = now() - timedelta(days=7)
    if request.GET.get("start_time"):
        try:
            start_time = add_tz_info(parse(request.GET.get("start_time")))
        except Exception as e:
            pass
    if request.GET.get("end_time"):
        try:
            end_time = add_tz_info(parse(request.GET.get("end_time")))
        except Exception as e:
            pass

    polls = []
    questions = Question.objects.filter(
            created_at__gt=start_time,
            created_at__lt=end_time)
    if request.GET.get("exclude_team_members"):
        questions = questions.exclude(username__in=settings.TEAM_MEMBERS)

    for question in questions:
        vote_count = 0
        already_counted_users = []
        for choice in question.choices.all():
            voted_users = choice.voted_users.all()
            for voted_user in voted_users:
                if voted_user.pk in already_counted_users:
                    continue
                vote_count += 1
                # now, with the multiple choices implemented
                # only one choice of a user should be counted, here.
                already_counted_users.append(voted_user.pk)
        polls.append({"vote_count": vote_count, "poll": question})

    polls = sorted(polls, key=lambda x: x["vote_count"], reverse=True)

    return render(request, "polls_by_vote.html", {
        "polls": polls, "start_time": start_time, "end_time": end_time})

@csrf_exempt
def vote_transaction_details(request):
    poll_id = request.POST.get("poll_id")
    choices = request.POST.getlist("choices[]")
    additional_thoughts = request.POST.get("additional_thoughts")
    username = request.POST.get("username")

    try:
        poll = Question.objects.get(pk=int(poll_id))
    except Question.DoesNotExist:
        raise Http404

    choice_instances = []
    for choice_id in choices:
        try:
            choice = Choice.objects.get(pk=int(choice_id))
        except Choice.DoesNotExist:
            raise Http404
        choice_instances.append(choice)

    choice_text = ""
    for c in choice_instances:
        choice_text += f" - {c.text.strip()}\n"

    body = f"Voted for \n {choice_text}"
    if additional_thoughts:
        body += f"\n\n{additional_thoughts}"
    permlink = str(uuid.uuid4())
    parent_author = poll.username
    parent_permlink = poll.permlink
    json_metadata = {
        "tags": settings.DEFAULT_TAGS,
        "app": f"dpoll/{settings.DPOLL_APP_VERSION}",
        "content_type": "poll_vote",
        "votes": [c.text.strip() for c in choice_instances],
    }

    return JsonResponse({
        "username": username,
        "permlink": permlink,
        "title": "",
        "body": body,
        "json_metadata": json_metadata,
        "parent_username": parent_author,
        "parent_permlink": parent_permlink,
        "comment_options": "",
    })


def sync_vote(request):
    trx_id = request.GET.get("trx_id")

    try:
        # block numbers must be integer
        block_num = int(request.GET.get("block_num"))
    except (TypeError, ValueError):
        return HttpResponse('Invalid block ID', status=400)

    c = LightsteemClient(nodes=["https://blurtd.privex.io"])
    block_data = c.get_block(block_num)
    if not block_data:
        # block data may return null if it's invalid
        return HttpResponse('Invalid block ID', status=400)

    vote_tx = None
    for transaction in block_data.get("transactions", []):
        if transaction.get("transaction_id") == trx_id:
            vote_tx = transaction
            break

    if not vote_tx:
        return HttpResponse('Invalid transaction ID', status=400)

    vote_op = None
    for op_type, op_value in transaction.get("operations", []):
        if op_type != "comment":
            continue
        vote_op = op_value

    if not vote_op:
        return HttpResponse("Couldn't find valid vote operation.", status=400)

    # validate json metadata
    if not vote_op.get("json_metadata"):
        return HttpResponse("json_metadata is missing.", status=400)

    json_metadata = json.loads(vote_op.get("json_metadata", ""))

    # json_metadata should indicate content type
    if json_metadata.get("content_type") != "poll_vote":
        return HttpResponse("content_type field is missing.", status=400)

    # check votes
    votes = json_metadata.get("votes", [])
    if not len(votes):
        return HttpResponse("votes field is missing.", status=400)

    # check the poll exists
    try:
        question = Question.objects.get(
            username=vote_op.get("parent_author"),
            permlink=vote_op.get("parent_permlink"),
        )
    except Question.DoesNotExist:
        return HttpResponse("parent_author/parent_permlink is not a poll.", status=400)

    # Validate the choice
    choices = Choice.objects.filter(
        question=question,
    )
    selected_choices = []
    for choice in choices:
        for user_vote in votes:
            if choice.text == user_vote:
                selected_choices.append(choice)

    if not selected_choices:
        return HttpResponse("Invalid choices in votes field.", status=400)

    # check if the user exists in our database
    # if it doesn't, create it.
    try:
        user = User.objects.get(username=vote_op.get("author"))
    except User.DoesNotExist:
        user = User.objects.create_user(
            username=vote_op.get("author"))
        user.save()

    # check if we already registered a vote from that user
    if Choice.objects.filter(
            voted_users__username=vote_op.get("author"),
            question=question).count() != 0:
        return HttpResponse("You have already voted on that poll.", status=400)

    # register the vote
    for selected_choice in selected_choices:
        selected_choice.voted_users.add(user)

    # add vote audit entry
    vote_audit = VoteAudit(
        question=question,
        voter=user,
        block_id=block_num,
        trx_id=trx_id
    )
    vote_audit.save()

    return HttpResponse("Vote is registered to the database.", status=200)


def vote_check(request):
    try:
        question = Question.objects.get(pk=request.GET.get("question_id"))
    except Question.DoesNotExist:
        raise Http404

    if not request.GET.get("voter_username"):
        raise Http404

    users = set()
    for choice in Choice.objects.filter(question=question):
        for voted_user in choice.voted_users.all():
            users.add(voted_user.username)

    if request.GET.get("voter_username") in users:
        return JsonResponse({"voted": True})
    else:
        return JsonResponse({"voted": False})
