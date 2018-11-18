# -*- coding: utf-8 -*-
"""Define view for the Kudos app.

Copyright (C) 2018 Gitcoin Core

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.

"""

import json
import logging
import re

from django.conf import settings
from django.contrib import messages
from django.contrib.postgres.search import SearchVector
from django.contrib.staticfiles.templatetags.staticfiles import static
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.utils import timezone
from django.http import HttpResponseForbidden
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from eth_utils import is_address, to_checksum_address, to_normalized_address
from web3 import Web3

from cacheops import cached_view_as
from dashboard.models import Activity, Profile
from dashboard.notifications import maybe_market_kudos_to_email
from dashboard.utils import get_web3
from dashboard.views import record_user_action
from gas.utils import recommend_min_gas_price_to_confirm_in_time
from git.utils import get_emails_master, get_github_primary_email
from ratelimit.decorators import ratelimit
from retail.helpers import get_ip

from .forms import KudosSearchForm
from .helpers import get_token
from .models import KudosTransfer, Token, BulkTransferCoupon, BulkTransferRedemption

logger = logging.getLogger(__name__)

confirm_time_minutes_target = 4


def get_profile(handle):
    """Get the gitcoin profile.
    TODO:  This might be depreacted in favor of the sync_profile function in the future.

    Args:
        handle (str): The github handle.

    Returns:
        obj: The profile model object.
    """
    try:
        to_profile = Profile.objects.get(handle__iexact=handle)
    except Profile.MultipleObjectsReturned:
        to_profile = Profile.objects.filter(handle__iexact=handle).order_by('-created_on').first()
    except Profile.DoesNotExist:
        to_profile = None
    return to_profile


@cached_view_as(
    Token.objects.select_related('contract').filter(
        num_clones_allowed__gt=0, contract__is_latest=True, contract__network=settings.KUDOS_NETWORK, hidden=False,
    )
)
def about(request):
    """Render the Kudos 'about' page."""
    listings = Token.objects.select_related('contract').filter(
        num_clones_allowed__gt=0,
        contract__is_latest=True,
        contract__network=settings.KUDOS_NETWORK,
        hidden=False,
    ).order_by('-created_on')
    context = {
        'is_outside': True,
        'active': 'about',
        'title': 'About Kudos',
        'card_title': _('Each Kudos is a unique work of art.'),
        'card_desc': _('It can be sent to highlight, recognize, and show appreciation.'),
        'avatar_url': static('v2/images/kudos/assets/kudos-image.png'),
        "listings": listings
    }
    return TemplateResponse(request, 'kudos_about.html', context)


def marketplace(request):
    """Render the Kudos 'marketplace' page."""
    q = request.GET.get('q')
    order_by = request.GET.get('order_by', '-created_on')
    logger.info(order_by)
    logger.info(q)
    title = q.title() + str(_(" Kudos ")) if q else str(_('Kudos Marketplace'))

    if q:
        listings = Token.objects.annotate(
            search=SearchVector('name', 'description', 'tags')
        ).select_related('contract').filter(
            # Only show the latest contract Kudos for the current network
            num_clones_allowed__gt=0,
            contract__is_latest=True,
            contract__network=settings.KUDOS_NETWORK,
            hidden=False,
            search=q
        ).order_by(order_by)
    else:
        listings = Token.objects.select_related('contract').filter(
            num_clones_allowed__gt=0,
            contract__is_latest=True,
            contract__network=settings.KUDOS_NETWORK,
            hidden=False,
        ).order_by(order_by)
    context = {
        'is_outside': True,
        'active': 'marketplace',
        'title': title,
        'card_title': _('Each Kudos is a unique work of art.'),
        'card_desc': _('It can be sent to highlight, recognize, and show appreciation.'),
        'avatar_url': static('v2/images/kudos/assets/kudos-image.png'),
        'listings': listings,
        'network': settings.KUDOS_NETWORK
    }

    return TemplateResponse(request, 'kudos_marketplace.html', context)


def search(request):
    """Render the search page.

    TODO:  This might no longer be used.

    """
    context = {}

    if request.method == 'GET':
        form = KudosSearchForm(request.GET)
        context = {'form': form}

    return TemplateResponse(request, 'kudos_marketplace.html', context)


def image(request, kudos_id, name):
    kudos = Token.objects.get(pk=kudos_id)
    img = kudos.as_img
    if not img:
        raise Http404

    response = HttpResponse(img.getvalue(), content_type='image/png')
    return response


def details_by_address_and_token_id(request, address, token_id, name):
    kudos = get_token(token_id=token_id, network=settings.KUDOS_NETWORK, address=address)
    return redirect(f'/kudos/{kudos.id}/{kudos.name}')


def details(request, kudos_id, name):
    """Render the Kudos 'detail' page."""
    if not re.match(r'\d+', kudos_id):
        raise ValueError(f'Invalid Kudos ID found.  ID is not a number:  {kudos_id}')

    # Find other profiles that have the same kudos name
    kudos = get_object_or_404(Token, pk=kudos_id)
    # Find other Kudos rows that are the same kudos.name, but of a different owner
    related_kudos = Token.objects.select_related('contract').filter(
        name=kudos.name,
        num_clones_allowed=0,
        contract__network=settings.KUDOS_NETWORK,
    )
    # Find the Wallet rows that match the Kudos.owner_addresses
    # related_wallets = Wallet.objects.filter(address__in=[rk.owner_address for rk in related_kudos]).distinct()[:20]

    # Find the related Profiles assuming the preferred_payout_address is the kudos owner address.
    # Note that preferred_payout_address is most likely in normalized form.
    # https://eth-utils.readthedocs.io/en/latest/utilities.html#to-normalized-address-value-text
    owner_addresses = [
        to_normalized_address(rk.owner_address) if is_address(rk.owner_address) is not False else None
        for rk in related_kudos
    ]
    related_profiles = Profile.objects.filter(preferred_payout_address__in=owner_addresses).distinct()[:20]
    # profile_ids = [rw.profile_id for rw in related_wallets]

    # Avatar can be accessed via Profile.avatar
    # related_profiles = Profile.objects.filter(pk__in=profile_ids).distinct()

    context = {
        'is_outside': True,
        'active': 'details',
        'title': 'Details',
        'card_title': _('Each Kudos is a unique work of art.'),
        'card_desc': _('It can be sent to highlight, recognize, and show appreciation.'),
        'avatar_url': static('v2/images/kudos/assets/kudos-image.png'),
        'kudos': kudos,
        'related_profiles': related_profiles,
    }
    if kudos:
        token = Token.objects.select_related('contract').get(
            token_id=kudos.cloned_from_id,
            contract__address=kudos.contract.address,
        )
        # The real num_cloned_in_wild is only stored in the Gen0 Kudos token
        kudos.num_clones_in_wild = token.num_clones_in_wild
        # Create a new attribute to reference number of gen0 clones allowed
        kudos.num_gen0_clones_allowed = token.num_clones_allowed

        context['title'] = kudos.humanized_name
        context['card_title'] = kudos.humanized_name
        context['card_desc'] = kudos.description
        context['avatar_url'] = kudos.img_url
        context['kudos'] = kudos

    return TemplateResponse(request, 'kudos_details.html', context)


def mint(request):
    """Render the Kudos 'mint' page.  This is mostly a placeholder for future functionality."""
    return TemplateResponse(request, 'kudos_mint.html', {})


def get_primary_from_email(params, request):
    """Find the primary_from_email address.  This function finds the address using this priority:

    1. If the email field is filed out in the Send POST request, use the `fromEmail` field.
    2. If the user is logged in, they should have an email address associated with their account.
        Use this as the second option.  `request_user_email`.
    3. If all else fails, attempt to pull the email from the user's github account.

    Args:
        params (dict): A dictionary parsed form the POST request.  Typically this is a POST
            request coming in from a Tips/Kudos send.

    Returns:
        str: The primary_from_email string.

    """

    request_user_email = request.user.email if request.user.is_authenticated else ''
    logger.info(request.user.profile)
    access_token = request.user.profile.get_access_token() if request.user.is_authenticated else ''

    if params.get('fromEmail'):
        primary_from_email = params['fromEmail']
    elif request_user_email:
        primary_from_email = request_user_email
    elif access_token:
        primary_from_email = get_github_primary_email(access_token)
    else:
        primary_from_email = 'unknown@gitcoin.co'

    return primary_from_email


def kudos_preferred_wallet(request, handle):
    """Returns the address, if any, that someone would like to be send kudos directly to."""
    response = {'addresses': []}
    profile = get_profile(str(handle).replace('@', ''))

    if profile and profile.preferred_payout_address:
        response['addresses'].append(profile.preferred_payout_address)

    return JsonResponse(response)


@ratelimit(key='ip', rate='5/m', method=ratelimit.UNSAFE, block=True)
def send_2(request):
    """Handle the first start of the Kudos email send.

    This form is filled out before the 'send' button is clicked.

    """
    _id = request.GET.get('id')
    kudos = Token.objects.filter(pk=_id).first()
    params = {
        'active': 'send',
        'issueURL': request.GET.get('source'),
        'class': 'send2',
        'recommend_gas_price': recommend_min_gas_price_to_confirm_in_time(confirm_time_minutes_target),
        'from_email': getattr(request.user, 'email', ''),
        'from_handle': request.user.username,
        'title': _('Send Kudos | Gitcoin'),
        'card_desc': _('Send a Kudos to any github user at the click of a button.'),
        'kudos': kudos,
    }
    return TemplateResponse(request, 'transaction/send.html', params)


@csrf_exempt
@ratelimit(key='ip', rate='5/m', method=ratelimit.UNSAFE, block=True)
def send_3(request):
    """Handle the third stage of sending a kudos (the POST).

    This function is derived from send_tip_3.
    The request to send the kudos is added to the database, but the transaction
    has not happened yet.  The txid is added in `send_kudos_4`.

    Returns:
        JsonResponse: The response with success state.

    """
    response = {
        'status': 'OK',
        'message': _('Kudos Created'),
    }

    is_user_authenticated = request.user.is_authenticated
    from_username = request.user.username if is_user_authenticated else ''
    primary_from_email = request.user.email if is_user_authenticated else ''
    access_token = request.user.profile.get_access_token() if is_user_authenticated and request.user.profile else ''
    to_emails = []

    params = json.loads(request.body)

    to_username = params.get('username', '').lstrip('@')
    to_emails = get_emails_master(to_username)

    email = params.get('email')
    if email:
        to_emails.append(email)

    # If no primary email in session, try the POST data. If none, fetch from GH.
    primary_from_email = params.get('fromEmail')

    if access_token and not primary_from_email:
        primary_from_email = get_github_primary_email(access_token)

    to_emails = list(set(to_emails))

    # Validate that the token exists on the back-end
    kudos_id = params.get('kudosId')
    if not kudos_id:
        raise Http404

    try:
        kudos_token_cloned_from = Token.objects.get(pk=kudos_id)
    except Token.DoesNotExist:
        raise Http404

    # db mutations
    KudosTransfer.objects.create(
        emails=to_emails,
        # For kudos, `token` is a kudos.models.Token instance.
        kudos_token_cloned_from=kudos_token_cloned_from,
        amount=params['amount'],
        comments_public=params['comments_public'],
        ip=get_ip(request),
        github_url=params['github_url'],
        from_name=params['from_name'],
        from_email=params['from_email'],
        from_username=from_username,
        username=params['username'],
        network=params['network'],
        tokenAddress=params['tokenAddress'],
        from_address=params['from_address'],
        is_for_bounty_fulfiller=params['is_for_bounty_fulfiller'],
        metadata=params['metadata'],
        recipient_profile=get_profile(to_username),
        sender_profile=get_profile(from_username),
    )

    return JsonResponse(response)


@csrf_exempt
@ratelimit(key='ip', rate='5/m', method=ratelimit.UNSAFE, block=True)
def send_4(request):
    """Handle the fourth stage of sending a tip (the POST).

    Once the metamask transaction is complete, add it to the database.

    Returns:
        JsonResponse: response with success state.

    """
    response = {
        'status': 'OK',
        'message': _('Kudos Sent'),
    }
    params = json.loads(request.body)
    from_username = request.user.username
    txid = params['txid']
    destination_account = params['destinationAccount']
    is_direct_to_recipient = params.get('is_direct_to_recipient', False)
    kudos_transfer = KudosTransfer.objects.get(
        metadata__address=destination_account,
        metadata__creation_time=params['creation_time'],
        metadata__salt=params['salt'],
    )

    # Return Permission Denied if not authenticated
    is_authenticated_via_login = (kudos_transfer.from_username and kudos_transfer.from_username == from_username)
    is_authenticated_for_this_via_ip = kudos_transfer.ip == get_ip(request)
    is_authed = is_authenticated_for_this_via_ip or is_authenticated_via_login

    if not is_authed:
        return JsonResponse({'status': 'error', 'message': _('Permission Denied')}, status=401)

    # Save the txid to the database once it has been confirmed in MetaMask.  If there is no txid,
    # it means that the user never went through with the transaction.
    kudos_transfer.txid = txid
    if is_direct_to_recipient:
        kudos_transfer.receive_txid = txid
    kudos_transfer.save()

    # notifications
    maybe_market_kudos_to_email(kudos_transfer)
    # record_user_action(kudos_transfer.from_username, 'send_kudos', kudos_transfer)
    # record_kudos_activity(kudos_transfer, kudos_transfer.from_username, 'new_kudos' if kudos_transfer.username else 'new_crowdfund')
    return JsonResponse(response)


def record_kudos_email_activity(kudos_transfer, github_handle, event_name):
    kwargs = {
        'activity_type': event_name,
        'kudos_transfer': kudos_transfer,
        'metadata': {
            'amount': str(kudos_transfer.amount),
            'token_name': kudos_transfer.tokenName,
            'value_in_eth': str(kudos_transfer.value_in_eth),
            'value_in_usdt_now': str(kudos_transfer.value_in_usdt_now),
            'github_url': kudos_transfer.github_url,
            'to_username': kudos_transfer.username,
            'from_name': kudos_transfer.from_name,
            'received_on': str(kudos_transfer.received_on) if kudos_transfer.received_on else None
        }
    }
    try:
        kwargs['profile'] = Profile.objects.get(handle=github_handle)
    except Profile.MultipleObjectsReturned:
        kwargs['profile'] = Profile.objects.filter(handle__iexact=github_handle).first()
    except Profile.DoesNotExist:
        logging.error(f"error in record_kudos_email_activity: profile with github name {github_handle} not found")
        return
    try:
        kwargs['bounty'] = kudos_transfer.bounty
    except KudosTransfer.DoesNotExist:
        logger.info('No bounty is associated with this kudos transfer.')

    try:
        Activity.objects.create(**kwargs)
    except Exception as e:
        logging.error(f"error in record_kudos_email_activity: {e} - {event_name} - {kudos_transfer} - {github_handle}")


def receive(request, key, txid, network):
    """Handle the receiving of a kudos (the POST).

    Returns:
        TemplateResponse: the UI with the kudos confirmed

    """
    these_kudos_emails = KudosTransfer.objects.filter(web3_type='v3', txid=txid, network=network)
    kudos_emails = these_kudos_emails.filter(metadata__reference_hash_for_receipient=key) | these_kudos_emails.filter(
        metadata__reference_hash_for_funder=key)
    kudos_transfer = kudos_emails.first()
    is_authed = kudos_transfer.trust_url or request.user.username.replace('@', '') in [
        kudos_transfer.username.replace('@', ''),
        kudos_transfer.from_username.replace('@', '')
    ]
    not_mined_yet = get_web3(kudos_transfer.network).eth.getBalance(
        Web3.toChecksumAddress(kudos_transfer.metadata['address'])) == 0

    if not request.user.is_authenticated or request.user.is_authenticated and not getattr(
        request.user, 'profile', None
    ):
        login_redirect = redirect('/login/github?next=' + request.get_full_path())
        return login_redirect

    if kudos_transfer.receive_txid:
        messages.info(request, _('This kudos has been received'))
    elif not is_authed:
        messages.error(
            request, f'This kudos is for {kudos_transfer.username} but you are logged in as {request.user.username}.  Please logout and log back in as {kudos_transfer.username}.')
    elif not_mined_yet and not request.GET.get('receive_txid'):
        message = mark_safe(
            f'The <a href="https://etherscan.io/tx/{txid}">transaction</a> is still mining.  '
            'Please wait a moment before submitting the receive form.'
        )
        messages.info(request, message)
    elif request.GET.get('receive_txid') and not kudos_transfer.receive_txid:
        params = request.GET

        # db mutations
        try:
            if params['save_addr']:
                profile = get_profile(kudos_transfer.username)
                if profile:
                    # TODO: Does this mean that the address the user enters in the receive form
                    # Will overwrite an already existing preferred_payout_address?  Should we
                    # ask the user to confirm this?
                    profile.preferred_payout_address = params['forwarding_address']
                    profile.save()
            kudos_transfer.receive_txid = params['receive_txid']
            kudos_transfer.receive_address = params['forwarding_address']
            kudos_transfer.received_on = timezone.now()
            kudos_transfer.save()
            record_user_action(kudos_transfer.from_username, 'receive_kudos', kudos_transfer)
            record_kudos_email_activity(kudos_transfer, kudos_transfer.username, 'receive_kudos')
            messages.success(request, _('This kudos has been received'))
        except Exception as e:
            messages.error(request, str(e))
            logger.exception(e)

    params = {
        'issueURL': request.GET.get('source'),
        'class': 'receive',
        'title': _('Receive Kudos'),
        'gas_price': round(recommend_min_gas_price_to_confirm_in_time(120), 1),
        'kudos_transfer': kudos_transfer,
        'key': key,
        'is_authed': is_authed,
        'disable_inputs': kudos_transfer.receive_txid or not_mined_yet or not is_authed,
    }

    return TemplateResponse(request, 'transaction/receive.html', params)


@ratelimit(key='ip', rate='10/m', method=ratelimit.UNSAFE, block=True)
def receive_bulk(request, secret):

    coupons = BulkTransferCoupon.objects.filter(secret=secret)
    if not coupons.exists():
        raise Http404

    coupon = coupons.first()
    redemptions = BulkTransferRedemption.objects.filter(redeemed_by=request.user.profile, coupon=coupon)
    if redemptions.exists():
        raise HttpResponseForbidden

    params = {
        'coupon': coupon,
    }
    return TemplateResponse(request, 'transaction/receive_bulk.html', params)