// Code generated by go-swagger; DO NOT EDIT.

package job_model

// This file was generated by the swagger tool.
// Editing this file might prove futile when you re-run the swagger generate command

import (
	strfmt "github.com/go-openapi/strfmt"

	"github.com/go-openapi/errors"
	"github.com/go-openapi/swag"
)

// V1Trigger Trigger defines what starts a pipeline run.
// swagger:model v1Trigger
type V1Trigger struct {

	// cron schedule
	CronSchedule *V1CronSchedule `json:"cron_schedule,omitempty"`

	// periodic schedule
	PeriodicSchedule *V1PeriodicSchedule `json:"periodic_schedule,omitempty"`
}

// Validate validates this v1 trigger
func (m *V1Trigger) Validate(formats strfmt.Registry) error {
	var res []error

	if err := m.validateCronSchedule(formats); err != nil {
		res = append(res, err)
	}

	if err := m.validatePeriodicSchedule(formats); err != nil {
		res = append(res, err)
	}

	if len(res) > 0 {
		return errors.CompositeValidationError(res...)
	}
	return nil
}

func (m *V1Trigger) validateCronSchedule(formats strfmt.Registry) error {

	if swag.IsZero(m.CronSchedule) { // not required
		return nil
	}

	if m.CronSchedule != nil {
		if err := m.CronSchedule.Validate(formats); err != nil {
			if ve, ok := err.(*errors.Validation); ok {
				return ve.ValidateName("cron_schedule")
			}
			return err
		}
	}

	return nil
}

func (m *V1Trigger) validatePeriodicSchedule(formats strfmt.Registry) error {

	if swag.IsZero(m.PeriodicSchedule) { // not required
		return nil
	}

	if m.PeriodicSchedule != nil {
		if err := m.PeriodicSchedule.Validate(formats); err != nil {
			if ve, ok := err.(*errors.Validation); ok {
				return ve.ValidateName("periodic_schedule")
			}
			return err
		}
	}

	return nil
}

// MarshalBinary interface implementation
func (m *V1Trigger) MarshalBinary() ([]byte, error) {
	if m == nil {
		return nil, nil
	}
	return swag.WriteJSON(m)
}

// UnmarshalBinary interface implementation
func (m *V1Trigger) UnmarshalBinary(b []byte) error {
	var res V1Trigger
	if err := swag.ReadJSON(b, &res); err != nil {
		return err
	}
	*m = res
	return nil
}
