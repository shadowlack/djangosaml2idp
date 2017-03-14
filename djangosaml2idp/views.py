# -*- coding: utf-8 -*-
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import copy
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.urlresolvers import reverse
from django.http import (HttpResponse, HttpResponseBadRequest,
                         HttpResponseRedirect, HttpResponseServerError)
from django.utils.datastructures import MultiValueDictKeyError
from django.views.decorators.csrf import csrf_exempt
from saml2 import BINDING_HTTP_POST, BINDING_HTTP_REDIRECT
from saml2.authn_context import PASSWORD, AuthnBroker, authn_context_class_ref
from saml2.config import IdPConfig
from saml2.ident import NameID
from saml2.metadata import entity_descriptor
from saml2.s_utils import UnknownPrincipal, UnsupportedBinding
from saml2.server import Server
from six import text_type

logger = logging.getLogger(__name__)


@csrf_exempt
def sso_entry(request):
    passed_data = request.POST if request.method == 'POST' else request.GET
    try:
        request.session['SAMLRequest'] = passed_data['SAMLRequest']
    except (KeyError, MultiValueDictKeyError) as e:
        return HttpResponseBadRequest(e)
    request.session['RelayState'] = passed_data.get('RelayState', '')
    # For HTTP-REDIRECT endpoint
    if "SigAlg" in passed_data and "Signature" in passed_data:
        request.session['SigAlg'] = passed_data['SigAlg']
        request.session['Signature'] = passed_data['Signature']
    return HttpResponseRedirect(reverse('saml_login_process'))


def create_identity(user, sp_mapping):
    identity = {}
    for user_attr, out_attr in sp_mapping.items():
        if hasattr(user, user_attr):
            identity[out_attr] = getattr(user, user_attr)
    return identity


# TODO Add http redirect logic based on https://github.com/rohe/pysaml2/blob/master/example/idp2_repoze/idp.py#L327
@login_required
def login_process(request):
    """
    Processor-based login continuation.
    Presents a SAML 2.0 Assertion for POSTing back to the Service Provider.
    """
    # Construct server with config from settings dict
    conf = IdPConfig()
    conf.load(copy.deepcopy(settings.SAML_IDP_CONFIG))
    IDP = Server(config=conf)
    # Parse incoming request
    try:
        req_info = IDP.parse_authn_request(request.session['SAMLRequest'], BINDING_HTTP_POST)
        _authn_req = req_info.message
    except Exception as excp:
        return HttpResponseBadRequest(excp)

    # Signed request for HTTP-REDIRECT
    if "SigAlg" in request.session and "Signature" in request.session:
        _certs = IDP.metadata.certs(_authn_req.issuer.text, "any", "signing")
        verified_ok = False
        for cert in _certs:
            # TODO implement
            #if verify_redirect_signature(_info, IDP.sec.sec_backend, cert):
            #    verified_ok = True
            #    break
            pass
        if not verified_ok:
            return HttpResponseBadRequest("Message signature verification failure")

    binding_out, destination = IDP.pick_binding(
        service="assertion_consumer_service",
        entity_id=_authn_req.issuer.text)

    # Gather response arguments
    try:
        resp_args = IDP.response_args(_authn_req)
    except (UnknownPrincipal, UnsupportedBinding) as excp:
        return HttpResponseServerError(excp)

    # Create Identity dict (SP-specific)
    try:
        sp_mapping = settings.SAML_IDP_ACS_ATTRIBUTE_MAPPING.get(resp_args['sp_entity_id'], {'username': 'username'})
    except AttributeError:
        identity = {'username': 'username'}
    identity = create_identity(request.user, sp_mapping)

    # TODO investigate how this works, because I don't get it
    req_authn_context = _authn_req.requested_authn_context or PASSWORD
    AUTHN_BROKER = AuthnBroker()
    AUTHN_BROKER.add(authn_context_class_ref(req_authn_context), "")

    # Construct SamlResponse message
    try:
        authn_resp = IDP.create_authn_response(
            identity=identity, userid=request.user.username,
            name_id=NameID(format=resp_args['name_id_policy'].format, sp_name_qualifier=destination, text=request.user.username),
            authn=AUTHN_BROKER.get_authn_by_accr(req_authn_context),
            sign_response=IDP.config.getattr("sign_response", "idp") or False,
            sign_assertion=IDP.config.getattr("sign_assertion", "idp") or False,
            **resp_args)
    except Exception as excp:
        return HttpResponseServerError(excp)

    # Return as html with self-submitting form.
    http_args = IDP.apply_binding(
        binding=binding_out,
        msg_str="%s" % authn_resp,
        destination=destination,
        relay_state=request.session['RelayState'],
        response=True)
    return HttpResponse(http_args['data'])


def metadata(request):
    """Returns an XML with the SAML 2.0 metadata for this Idp as configured in the settings.py file.
    """
    conf = IdPConfig()
    conf.load(copy.deepcopy(settings.SAML_IDP_CONFIG))
    metadata = entity_descriptor(conf)
    return HttpResponse(content=text_type(metadata).encode('utf-8'), content_type="text/xml; charset=utf8")
