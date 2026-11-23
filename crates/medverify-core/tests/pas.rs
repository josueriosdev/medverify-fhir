use jiff::{civil::date, Timestamp, Zoned};
use medverify_core::fhir_pas::*;
use serde_json::{json, Value};

const PAS: &str = "http://hl7.org/fhir/us/davinci-pas/StructureDefinition/";
const BACKPORT: &str = "http://hl7.org/fhir/uv/subscriptions-backport/StructureDefinition/";
const TOPIC: &str = "http://hl7.org/fhir/us/davinci-pas/SubscriptionTopic/PASSubscriptionTopic";
const MEDICAL: &str = "https://example.test/synthetic-procedures";

fn request() -> Value {
    serde_json::from_str(include_str!("fixtures/pas/request.json")).unwrap()
}

fn response() -> Value {
    serde_json::from_str(include_str!("fixtures/pas/response.json")).unwrap()
}

fn set_action(line: &mut Value, code: &str) {
    line["adjudication"][0]["extension"][0]["extension"][0]["valueCodeableConcept"]["coding"][0]
        ["code"] = json!(code);
}

fn medical(system: &str, _: &str) -> Option<ServiceKind> {
    (system == MEDICAL).then_some(ServiceKind::Medical)
}

#[test]
fn collection_validates_profiles_and_inline_references() {
    let mut value = request();
    let parsed = validate_request(&value).unwrap();
    assert_eq!(parsed.priority(), Priority::Standard);
    assert_eq!(
        parsed.items().map(|item| item.sequence).collect::<Vec<_>>(),
        [1, 5]
    );

    value["entry"][0]["resource"]["extension"] = json!([{
        "url": format!("{PAS}extension-levelOfServiceCode"),
        "valueCodeableConcept": {"coding":[{"system":"https://codesystem.x12.org/005010/1338","code":"U"}]}
    }]);
    assert_eq!(
        validate_request(&value).unwrap().priority(),
        Priority::Expedited
    );

    let mut malformed = value.clone();
    malformed["type"] = json!("transaction");
    assert!(matches!(validate_request(&malformed), Err(Error::Bundle)));
    malformed = value.clone();
    malformed["entry"].as_array_mut().unwrap().swap(0, 1);
    assert!(matches!(
        validate_request(&malformed),
        Err(Error::Profile(_))
    ));
    malformed = value.clone();
    malformed["entry"][0]["resource"]["use"] = json!("claim");
    assert!(matches!(
        validate_request(&malformed),
        Err(Error::Profile(_))
    ));
    malformed = value.clone();
    malformed["entry"][0]["resource"]["meta"]["profile"][0] =
        json!(format!("{PAS}profile-claim|2.1.0"));
    assert!(matches!(
        validate_request(&malformed),
        Err(Error::Profile(_))
    ));
    malformed = value.clone();
    malformed["entry"][0]["resource"]["item"][0]["extension"][1]["valueCodeableConcept"] =
        json!({"text":"initial"});
    assert!(matches!(
        validate_request(&malformed),
        Err(Error::Profile(_))
    ));
    malformed = value.clone();
    malformed["entry"][4]["resource"]["identifier"][0]["type"] = json!({"text":"Policy number"});
    assert!(matches!(
        validate_request(&malformed),
        Err(Error::Profile(_))
    ));
    malformed = value.clone();
    malformed["entry"][0]["resource"]["item"][0]["informationSequence"] = json!([1]);
    assert!(matches!(
        validate_request(&malformed),
        Err(Error::ItemCorrelation)
    ));
    malformed = value.clone();
    malformed["entry"][0]["resource"]["patient"]["reference"] = json!("Organization/payer");
    assert!(matches!(
        validate_request(&malformed),
        Err(Error::ReferenceType)
    ));
    malformed = value.clone();
    malformed["entry"][1]["fullUrl"] = json!("https://another.test/fhir/Patient/patient");
    assert!(matches!(
        validate_request(&malformed),
        Err(Error::Reference)
    ));
    let duplicate = value["entry"][1].clone();
    value["entry"].as_array_mut().unwrap().push(duplicate);
    assert!(matches!(
        validate_request(&value),
        Err(Error::DuplicateResource)
    ));
}

#[test]
fn references_resolve_through_extensions_and_match_versioned_and_urn_targets() {
    let mut value = request();
    value["entry"][0]["resource"]["item"][0]["extension"].as_array_mut().unwrap().push(
        json!({"url":format!("{PAS}extension-requestedService"),"valueReference":{"reference":"ServiceRequest/synthetic/_history/2"}})
    );
    value["entry"].as_array_mut().unwrap().push(json!({
        "fullUrl":"https://example.test/fhir/ServiceRequest/synthetic",
        "resource":{
            "resourceType":"ServiceRequest","id":"synthetic","meta":{"versionId":"2"},
            "status":"active","intent":"order","subject":{"reference":"Patient/patient"}
        }
    }));
    assert!(validate_request(&value).is_ok());
    value["entry"][5]["resource"]["meta"]["versionId"] = json!("3");
    assert!(matches!(validate_request(&value), Err(Error::Reference)));

    let mut urns = request();
    let patient = "urn:uuid:557a5ba5-8ac5-4b42-a02b-e01cc23852c9";
    urns["entry"][1]["fullUrl"] = json!(patient);
    urns["entry"][0]["resource"]["patient"]["reference"] = json!(patient);
    urns["entry"][4]["resource"]["beneficiary"]["reference"] = json!(patient);
    assert!(validate_request(&urns).is_ok());
    urns["entry"][4]["resource"]["beneficiary"]["reference"] = json!("Patient/patient");
    assert!(matches!(validate_request(&urns), Err(Error::Reference)));
}

#[test]
fn claim_update_requires_its_previous_claim_and_preserves_line_numbers() {
    let mut value = request();
    let mut previous = value["entry"][0].clone();
    previous["fullUrl"] = json!("https://example.test/fhir/Claim/previous");
    previous["resource"]["id"] = json!("previous");
    value["entry"][0]["resource"]["meta"]["profile"][0] =
        json!(format!("{PAS}profile-claim-update"));
    value["entry"][0]["resource"]["related"] = json!([{
        "claim":{"reference":"Claim/previous"},
        "relationship":{"coding":[{"system":"http://terminology.hl7.org/CodeSystem/ex-relatedclaimrelationship","code":"prior"}]}
    }]);
    assert!(matches!(
        validate_request(&value),
        Err(Error::ClaimCorrelation)
    ));
    value["entry"].as_array_mut().unwrap().push(previous);
    assert!(validate_request(&value).is_ok());
    value["entry"][0]["resource"]["item"]
        .as_array_mut()
        .unwrap()
        .pop();
    assert!(matches!(
        validate_request(&value),
        Err(Error::ItemCorrelation)
    ));
}

#[test]
fn response_matches_claim_by_reference_or_identifier_and_correlates_original_lines() {
    let submission = request();
    let request = validate_request(&submission).unwrap();
    let mut value = response();
    let parsed = validate_response(&value, &request, ResponseContext::default()).unwrap();
    assert_eq!(parsed.action(1), Some(Action::Approved));
    assert_eq!(parsed.action(5), Some(Action::Approved));
    assert_eq!(parsed.action(2), None);
    value["entry"][0]["resource"]["request"] = json!({
        "identifier":{"system":"https://example.test/identifiers","value":"prior-auth-request"}
    });
    assert!(validate_response(&value, &request, ResponseContext::default()).is_ok());
    let mut wrong = value.clone();
    wrong["entry"][0]["resource"]["request"]["identifier"]["value"] = json!("other-request");
    assert!(matches!(
        validate_response(&wrong, &request, ResponseContext::default()),
        Err(Error::ClaimCorrelation)
    ));
    wrong = value.clone();
    wrong["entry"][0]["resource"]["item"][0]["itemSequence"] = json!(1);
    assert!(matches!(
        validate_response(&wrong, &request, ResponseContext::default()),
        Err(Error::ItemCorrelation)
    ));
    wrong = value.clone();
    wrong["entry"][0]["resource"]["item"][0]["adjudication"][0]["extension"][0]["extension"]
        .as_array_mut()
        .unwrap()
        .pop();
    assert!(matches!(
        validate_response(&wrong, &request, ResponseContext::default()),
        Err(Error::ApprovalNumber)
    ));
    wrong = value.clone();
    wrong["entry"].as_array_mut().unwrap().pop();
    assert!(matches!(
        validate_response(&wrong, &request, ResponseContext::default()),
        Err(Error::Reference)
    ));

    value["entry"][0]["resource"]["outcome"] = json!("error");
    value["entry"][0]["resource"]
        .as_object_mut()
        .unwrap()
        .remove("item");
    value["entry"][0]["resource"]["error"] = json!([{"code":{"text":"Invalid transaction"}}]);
    assert!(
        validate_response(&value, &request, ResponseContext::default())
            .unwrap()
            .business_error()
    );
}

#[test]
fn denial_requires_a_specific_coded_reason_or_a_line_linked_note() {
    let submission = request();
    let request = validate_request(&submission).unwrap();
    let mut value = response();
    set_action(&mut value["entry"][0]["resource"]["item"][1], "A3");
    assert!(matches!(
        validate_response(&value, &request, ResponseContext::default()),
        Err(Error::DenialReason)
    ));
    value["entry"][0]["resource"]["item"][1]["noteNumber"] = json!([7]);
    value["entry"][0]["resource"]["processNote"] = json!([{
        "number":7,"text":"The request does not document the required baseline assessment."
    }]);
    value["entry"][0]["resource"]["extension"] = json!([{
        "url":format!("{PAS}extension-claimResponseReviewer"),
        "extension":[{"url":"wasHumanReviewedFlag","valueBoolean":true}]
    }]);
    let result = validate_response(&value, &request, ResponseContext::default()).unwrap();
    assert_eq!(result.action(1), Some(Action::Denied));
    assert_eq!(result.human_reviewed(), Some(true));
    value["entry"][0]["resource"]["processNote"][0]["number"] = json!(8);
    assert!(matches!(
        validate_response(&value, &request, ResponseContext::default()),
        Err(Error::DenialReason)
    ));
    let reasons = value["entry"][0]["resource"]["item"][1]["adjudication"][0]["extension"][0]
        ["extension"]
        .as_array_mut()
        .unwrap();
    reasons.push(json!({
        "url":"reasonCode", "valueCodeableConcept":{"coding":[{"system":"https://example.test/denial-reason","code":"baseline-missing"}]}
    }));
    assert!(validate_response(&value, &request, ResponseContext::default()).is_ok());
}

#[test]
fn pending_for_additional_information_requires_a_linked_communication_request() {
    let submission = request();
    let request = validate_request(&submission).unwrap();
    let mut value = response();
    set_action(&mut value["entry"][0]["resource"]["item"][1], "A4");
    assert_eq!(
        validate_response(&value, &request, ResponseContext::default())
            .unwrap()
            .action(1),
        Some(Action::Pending)
    );
    let context = || ResponseContext {
        additional_information_requested: &[1],
    };
    assert!(matches!(
        validate_response(&value, &request, context()),
        Err(Error::PendingInformation)
    ));
    value["entry"][0]["resource"]["communicationRequest"] =
        json!([{"reference":"CommunicationRequest/more"}]);
    value["entry"].as_array_mut().unwrap().push(json!({
        "fullUrl":"https://example.test/fhir/CommunicationRequest/more",
        "resource":{
            "resourceType":"CommunicationRequest","id":"more","status":"active",
            "extension":[{"url":format!("{PAS}extension-serviceLineNumber"),"valuePositiveInt":1}],
            "payload":[{"contentString":"Provide the baseline assessment."}]
        }
    }));
    assert!(validate_response(&value, &request, context()).is_ok());
    value["entry"][3]["resource"]["extension"][0]["valuePositiveInt"] = json!(5);
    assert!(matches!(
        validate_response(&value, &request, context()),
        Err(Error::PendingInformation)
    ));
    let communication = &mut value["entry"][3]["resource"];
    let misplaced = communication
        .as_object_mut()
        .unwrap()
        .remove("extension")
        .unwrap();
    communication["payload"][0]["extension"] = misplaced;
    assert!(matches!(
        validate_response(&value, &request, context()),
        Err(Error::PendingInformation)
    ));
    value["entry"][3]["resource"]["payload"][0]["extension"][0]["valuePositiveInt"] = json!(1);
    assert!(validate_response(&value, &request, context()).is_ok());
}

#[test]
fn modified_items_require_authorized_details_linked_to_the_original_sequence() {
    let submission = request();
    let request = validate_request(&submission).unwrap();
    let mut value = response();
    set_action(&mut value["entry"][0]["resource"]["item"][1], "A6");
    assert!(matches!(
        validate_response(&value, &request, ResponseContext::default()),
        Err(Error::Modification)
    ));
    let authorized = value["entry"][0]["resource"]["item"][0]["adjudication"].clone();
    value["entry"][0]["resource"]["addItem"] = json!([{
        "itemSequence":[1], "adjudication":authorized,
        "productOrService":{"coding":[{"system":MEDICAL,"code":"Y9016"}]}
    }]);
    assert_eq!(
        validate_response(&value, &request, ResponseContext::default())
            .unwrap()
            .action(1),
        Some(Action::Modified)
    );
    value["entry"][0]["resource"]["addItem"][0]["itemSequence"] = json!([5]);
    assert!(matches!(
        validate_response(&value, &request, ResponseContext::default()),
        Err(Error::Modification)
    ));
}

fn zoned(time: &str) -> Zoned {
    time.parse().unwrap()
}

fn ts(time: &str) -> Timestamp {
    time.parse().unwrap()
}

fn timeline(received: &Zoned, plan: Plan, priority: Priority) -> Timeline<'_> {
    Timeline {
        payer: Payer {
            plan,
            rating_period_start: Some(date(2026, 1, 1)),
        },
        received,
        priority,
        checked_at: ts("2026-12-01T00:00:00Z"),
        extension: None,
        earlier_deadline: None,
    }
}

#[test]
fn cms_rule_excludes_drug_items_individually_and_applies_to_covered_payers() {
    let received = zoned("2026-07-06T09:00:00-07:00[America/Los_Angeles]");
    let submission = request();
    let request = validate_request(&submission).unwrap();
    let items: Vec<_> = request.items().collect();
    let timeline = timeline(&received, Plan::MedicareAdvantage, request.priority());
    let event = Some(CommunicatedAction {
        action: Action::Approved,
        at: ts("2026-07-07T09:00:00-07:00"),
    });
    assert!(matches!(
        timeline.assess_item(items[0], medical, event),
        Ok(Timeliness::OnTime { .. })
    ));
    assert_eq!(
        timeline.assess_item(items[1], |_, _| unreachable!(), event),
        Ok(Timeliness::OutsideScope(TimingScope::DrugExcluded))
    );
    assert_eq!(
        timeline.assess_item(items[0], |_, _| None, event),
        Ok(Timeliness::OutsideScope(TimingScope::UnclassifiedService))
    );

    for plan in [
        Plan::OriginalMedicare,
        Plan::FederalExchangeQhp,
        Plan::StateRegulatedCommercial,
        Plan::SelfFundedErisa,
    ] {
        let payer = Payer {
            plan,
            rating_period_start: None,
        };
        assert_eq!(
            payer.timing_scope(date(2026, 7, 6), Some(ServiceKind::Medical)),
            Ok(TimingScope::PayerExcluded)
        );
    }
    for plan in [
        Plan::MedicareAdvantage,
        Plan::ApplicableIntegratedPlan,
        Plan::MedicaidFfs,
        Plan::MedicaidManagedCare,
        Plan::ChipFfs,
        Plan::ChipManagedCare,
    ] {
        let payer = Payer {
            plan,
            rating_period_start: Some(date(2026, 3, 1)),
        };
        assert_eq!(
            payer.timing_scope(date(2026, 3, 1), Some(ServiceKind::Medical)),
            Ok(TimingScope::Applies)
        );
        assert_eq!(
            payer.timing_scope(date(2025, 12, 31), Some(ServiceKind::Medical)),
            Ok(TimingScope::BeforeEffectiveDate)
        );
    }
    let payer = Payer {
        plan: Plan::MedicaidManagedCare,
        rating_period_start: Some(date(2025, 8, 1)),
    };
    assert_eq!(
        payer.timing_scope(date(2026, 7, 6), Some(ServiceKind::Medical)),
        Ok(TimingScope::BeforeEffectiveDate)
    );
}

#[test]
fn expedited_uses_elapsed_hours_and_standard_uses_local_calendar_days_through_dst() {
    for (received, expedited, standard) in [
        (
            "2026-03-06T09:00:00-08:00[America/Los_Angeles]",
            "2026-03-09T10:00:00-07:00",
            "2026-03-13T09:00:00-07:00",
        ),
        (
            "2026-10-30T09:00:00-07:00[America/Los_Angeles]",
            "2026-11-02T08:00:00-08:00",
            "2026-11-06T09:00:00-08:00",
        ),
    ] {
        let received = zoned(received);
        for (priority, boundary) in [
            (Priority::Expedited, expedited),
            (Priority::Standard, standard),
        ] {
            let timeline = timeline(&received, Plan::MedicareAdvantage, priority);
            let deadline = ts(boundary);
            let just_late = deadline
                .checked_add(jiff::SignedDuration::from_nanos(1))
                .unwrap();
            let result = |at| {
                timeline.assess(
                    Some(ServiceKind::Medical),
                    Some(CommunicatedAction {
                        action: Action::Approved,
                        at,
                    }),
                )
            };
            assert_eq!(result(deadline), Ok(Timeliness::OnTime { deadline }));
            assert_eq!(result(just_late), Ok(Timeliness::Late { deadline }));
            let pending = Some(CommunicatedAction {
                action: Action::Pending,
                at: deadline,
            });
            assert_eq!(
                Timeline {
                    checked_at: deadline,
                    ..timeline
                }
                .assess(Some(ServiceKind::Medical), pending),
                Ok(Timeliness::AwaitingDecision { deadline })
            );
            assert_eq!(
                timeline.assess(Some(ServiceKind::Medical), pending),
                Ok(Timeliness::Overdue { deadline })
            );
        }
    }
}

#[test]
fn extensions_depend_on_payer_priority_reason_notice_and_elapsed_time() {
    let received = zoned("2026-10-30T09:00:00-07:00[America/Los_Angeles]");
    let timeline = timeline(&received, Plan::MedicareAdvantage, Priority::Expedited);
    let granted_at = ts("2026-11-01T09:00:00-08:00");
    let extension = Extension {
        calendar_days: 14,
        granted_at,
        reason: ExtensionReason::EnrolleeRequested,
        notice: Some(ExtensionNotice {
            sent_at: granted_at,
            written_reason: true,
            grievance_rights: Some(GrievanceRights::Expedited),
        }),
    };
    let assess = |timeline: Timeline<'_>, extension| {
        Timeline {
            extension: Some(extension),
            ..timeline
        }
        .assess(Some(ServiceKind::Medical), None)
    };
    let deadline = ts("2026-11-16T08:00:00-08:00");
    assert_eq!(
        assess(timeline, extension),
        Ok(Timeliness::Overdue { deadline })
    );
    assert_eq!(
        assess(
            timeline,
            Extension {
                calendar_days: 15,
                ..extension
            }
        ),
        Err(TimingError::InvalidExtension)
    );
    assert_eq!(
        assess(
            timeline,
            Extension {
                notice: None,
                ..extension
            }
        ),
        Err(TimingError::MissingExtensionNotice)
    );
    assert_eq!(
        assess(
            timeline,
            Extension {
                granted_at: ts("2026-11-02T08:00:01-08:00"),
                ..extension
            }
        ),
        Err(TimingError::InvalidExtension)
    );
    let ffs = Timeline {
        payer: Payer {
            plan: Plan::MedicaidFfs,
            ..timeline.payer
        },
        ..timeline
    };
    assert_eq!(assess(ffs, extension), Err(TimingError::InvalidExtension));
    let standard = Timeline {
        priority: Priority::Standard,
        ..ffs
    };
    let extension = Extension {
        reason: ExtensionReason::ProviderRequested,
        notice: None,
        ..extension
    };
    assert_eq!(
        assess(standard, extension),
        Ok(Timeliness::Overdue {
            deadline: ts("2026-11-20T09:00:00-08:00")
        })
    );
    let managed = Timeline {
        payer: Payer {
            plan: Plan::MedicaidManagedCare,
            ..standard.payer
        },
        ..standard
    };
    assert_eq!(
        assess(managed, extension),
        Err(TimingError::MissingExtensionNotice)
    );
    let managed_notice = ExtensionNotice {
        sent_at: granted_at,
        written_reason: true,
        grievance_rights: Some(GrievanceRights::Standard),
    };
    assert_eq!(
        assess(
            managed,
            Extension {
                notice: Some(managed_notice),
                ..extension
            }
        ),
        Ok(Timeliness::Overdue {
            deadline: ts("2026-11-20T09:00:00-08:00")
        })
    );
    let early = ts("2026-11-02T00:00:00-08:00");
    assert_eq!(
        assess(
            Timeline {
                earlier_deadline: Some(early),
                ..standard
            },
            extension
        ),
        Ok(Timeliness::Overdue { deadline: early })
    );
}

#[test]
fn rest_hook_subscription_is_scoped_to_its_sending_system_and_uses_full_resources() {
    let mut subscription = json!({
        "resourceType":"Subscription","meta":{"profile":[format!("{PAS}profile-subscription")]},
        "status":"requested", "reason":"Receive changes to prior authorizations.", "criteria":TOPIC,
        "_criteria":{"extension":[{"url":format!("{BACKPORT}backport-filter-criteria"),"valueString":"org-identifier=sample-clinic"}]},
        "channel":{"type":"rest-hook","endpoint":"https://example.test/callback","payload":"application/fhir+json",
            "_payload":{"extension":[{"url":format!("{BACKPORT}backport-payload-content"),"valueCode":"full-resource"}]}}
    });
    assert_eq!(
        validate_subscription(&subscription, &["sample-clinic"]),
        Ok(())
    );
    assert_eq!(
        validate_subscription(&subscription, &["different-clinic"]),
        Err(Error::Subscription)
    );
    subscription["channel"]["_payload"]["extension"][0]["valueCode"] = json!("id-only");
    assert_eq!(
        validate_subscription(&subscription, &["sample-clinic"]),
        Err(Error::Subscription)
    );
    assert_eq!(validate_inquiry(Inquiry::Manual), Ok(()));
    assert_eq!(
        validate_inquiry(Inquiry::AutomatedPolling),
        Err(Error::Subscription)
    );
}

#[test]
fn event_notification_correlates_subscription_and_embeds_a_complete_pas_response() {
    let request_value = request();
    let request = validate_request(&request_value).unwrap();
    let mut event = json!({
        "resourceType":"Bundle","type":"history","timestamp":"2026-07-06T09:07:00-07:00",
        "entry":[
            {"fullUrl":"https://example.test/fhir/Parameters/status","resource":{
                "resourceType":"Parameters","parameter":[
                    {"name":"topic","valueCanonical":TOPIC},
                    {"name":"type","valueCode":"event-notification"},
                    {"name":"subscription","valueReference":{"reference":"Subscription/notify"}},
                    {"name":"notification-event","part":[
                        {"name":"event-number","valueString":"1"},
                        {"name":"focus","valueReference":{"reference":"https://example.test/fhir/Bundle/response"}}
                    ]}
                ]
            }},
            {"fullUrl":"https://example.test/fhir/Bundle/response","resource":response()}
        ]
    });
    let check_response = |payload: &Value| {
        validate_response(payload, &request, ResponseContext::default()).map(|_| ())
    };
    assert_eq!(
        validate_notification(&event, "Subscription/notify", check_response),
        Ok(())
    );
    assert_eq!(
        validate_notification(&event, "Subscription/different", check_response),
        Err(Error::Notification)
    );
    event["entry"][1]["resource"]["entry"] = json!([]);
    assert!(validate_notification(&event, "Subscription/notify", check_response).is_err());
    event["entry"].as_array_mut().unwrap().pop();
    event["entry"][0]["resource"]["parameter"]
        .as_array_mut()
        .unwrap()
        .pop();
    for notification in ["handshake", "heartbeat"] {
        event["entry"][0]["resource"]["parameter"][1]["valueCode"] = json!(notification);
        assert_eq!(
            validate_notification(&event, "Subscription/notify", |_| unreachable!()),
            Ok(())
        );
    }
}

#[test]
fn state_reviewer_rules_depend_on_state_plan_date_and_decision_basis() {
    let rules = ReviewerPolicy::state_rules();
    let reviewer = ReviewerEvidence {
        profession: Profession::Physician,
        licensed_and_in_scope: true,
        individually_reviewed: true,
        independent_judgment: true,
        competent_for_service: true,
        actual_clinical_experience: true,
        board_certified_or_eligible_in_same_specialty: true,
        medical_director: false,
    };
    let context = ReviewContext {
        state: "TX",
        plan: Plan::StateRegulatedCommercial,
        on: date(2026, 7, 1),
        issued_or_renewed: Some(date(2026, 1, 1)),
        decision: Decision::Denial,
        basis: Basis::MedicalNecessity,
        specialty: Specialty::Medical,
    };
    use Automation::*;
    assert_eq!(
        rules.verify(context, ContributedToDecision, Some(reviewer)),
        Err(ReviewError::ProhibitedAutomation)
    );
    let older_plan = ReviewContext {
        issued_or_renewed: Some(date(2025, 12, 31)),
        ..context
    };
    assert_eq!(
        rules.verify(older_plan, ContributedToDecision, Some(reviewer)),
        Ok(())
    );
    assert_eq!(
        rules.verify(context, NoneOrAdministrativeSupport, Some(reviewer)),
        Ok(())
    );
    assert_eq!(
        rules.verify(
            ReviewContext {
                issued_or_renewed: None,
                ..context
            },
            NoneOrAdministrativeSupport,
            Some(reviewer)
        ),
        Err(ReviewError::MissingPlanDate)
    );

    let wa = ReviewContext {
        state: "WA",
        plan: Plan::StatePublicEmployees,
        ..context
    };
    assert_eq!(
        rules.verify(wa, ContributedToDecision, None),
        Err(ReviewError::UnqualifiedReviewer)
    );
    assert_eq!(
        rules.verify(
            ReviewContext {
                on: date(2026, 6, 10),
                ..wa
            },
            ContributedToDecision,
            None
        ),
        Ok(())
    );
    assert_eq!(
        rules.verify(
            ReviewContext {
                on: date(2026, 6, 11),
                ..wa
            },
            SoleDecisionMaker,
            Some(reviewer)
        ),
        Err(ReviewError::ProhibitedAutomation)
    );
    assert_eq!(
        rules.verify(
            ReviewContext {
                decision: Decision::Approval,
                ..wa
            },
            SoleDecisionMaker,
            None
        ),
        Ok(())
    );

    let ca = ReviewContext {
        state: "CA",
        decision: Decision::Delay,
        ..context
    };
    assert_eq!(
        rules.verify(ca, NoneOrAdministrativeSupport, None),
        Err(ReviewError::UnqualifiedReviewer)
    );
    assert_eq!(
        rules.verify(ca, ContributedToDecision, Some(reviewer)),
        Err(ReviewError::ProhibitedAutomation)
    );
    assert_eq!(
        rules.verify(
            ReviewContext {
                decision: Decision::Approval,
                ..ca
            },
            SoleDecisionMaker,
            None
        ),
        Err(ReviewError::ProhibitedAutomation)
    );
    assert_eq!(
        rules.verify(
            ReviewContext {
                basis: Basis::Administrative,
                ..ca
            },
            NoneOrAdministrativeSupport,
            None
        ),
        Ok(())
    );
    assert_eq!(
        rules.verify(
            ReviewContext {
                plan: Plan::SelfFundedErisa,
                ..ca
            },
            NoneOrAdministrativeSupport,
            None
        ),
        Ok(())
    );

    let md = ReviewContext {
        state: "MD",
        decision: Decision::Modification,
        plan: Plan::MedicaidManagedCare,
        ..context
    };
    let wrong_specialty = ReviewerEvidence {
        board_certified_or_eligible_in_same_specialty: false,
        ..reviewer
    };
    assert_eq!(
        rules.verify(md, ContributedToDecision, Some(wrong_specialty)),
        Err(ReviewError::UnqualifiedReviewer)
    );
    assert_eq!(
        rules.verify(
            ReviewContext {
                specialty: Specialty::MentalHealthOrSubstanceUse,
                ..md
            },
            ContributedToDecision,
            Some(wrong_specialty)
        ),
        Ok(())
    );
    assert_eq!(
        rules.verify(
            ReviewContext {
                specialty: Specialty::Dental,
                ..md
            },
            ContributedToDecision,
            Some(ReviewerEvidence {
                profession: Profession::Dentist,
                ..reviewer
            })
        ),
        Ok(())
    );

    let az = ReviewContext {
        state: "AZ",
        ..context
    };
    assert_eq!(
        rules.verify(az, NoneOrAdministrativeSupport, Some(reviewer)),
        Err(ReviewError::UnqualifiedReviewer)
    );
    assert_eq!(
        rules.verify(
            az,
            ContributedToDecision,
            Some(ReviewerEvidence {
                medical_director: true,
                ..reviewer
            })
        ),
        Ok(())
    );
    assert_eq!(
        rules.verify(
            ReviewContext {
                on: date(2026, 6, 30),
                ..az
            },
            NoneOrAdministrativeSupport,
            None
        ),
        Ok(())
    );
}
